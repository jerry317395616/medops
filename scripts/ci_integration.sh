#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="${BENCH_DIR:?Set BENCH_DIR to an empty CI workspace path}"
SITE_NAME="${SITE_NAME:-hrp-test.localhost}"
DEVELOPMENT_SITE_NAME="${DEVELOPMENT_SITE_NAME:-hrp-dev.localhost}"
DEMO_SITE_NAME="${DEMO_SITE_NAME:-hrp-demo.localhost}"
DB_TYPE="${DB_TYPE:-mariadb}"
DB_ROOT_USERNAME="${DB_ROOT_USERNAME:-root}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
REDIS_CACHE_PORT="${REDIS_CACHE_PORT:-13000}"
REDIS_QUEUE_PORT="${REDIS_QUEUE_PORT:-11000}"

: "${DB_ROOT_PASSWORD:?Set DB_ROOT_PASSWORD}"
: "${ADMIN_PASSWORD:?Set ADMIN_PASSWORD}"

if [[ -e "$BENCH_DIR" ]]; then
  echo "CI Bench directory already exists: $BENCH_DIR" >&2
  exit 1
fi

export PATH="$HOME/.local/bin:$PATH"

stop_redis() {
  redis-cli -p "$REDIS_CACHE_PORT" shutdown nosave >/dev/null 2>&1 || true
  redis-cli -p "$REDIS_QUEUE_PORT" shutdown nosave >/dev/null 2>&1 || true
}

trap stop_redis EXIT

export KEEP_TEMPORARY_REDIS=1
bash "$ROOT_DIR/scripts/bootstrap_latest_develop.sh"

for port in "$REDIS_CACHE_PORT" "$REDIS_QUEUE_PORT"; do
  if [[ "$(redis-cli -p "$port" ping)" != "PONG" ]]; then
    echo "CI Redis service on port $port is not ready" >&2
    exit 1
  fi
done

cd "$BENCH_DIR"

"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/version_lock.py" --bench "$BENCH_DIR"

FIRST_ENVIRONMENT_LOG="$BENCH_DIR/logs/environment-configure-first.log"
SECOND_ENVIRONMENT_LOG="$BENCH_DIR/logs/environment-configure-second.log"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" \
  configure test \
  --bench-dir "$BENCH_DIR" \
  --site-name "$SITE_NAME" \
  --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-test-first" \
  | tee "$FIRST_ENVIRONMENT_LOG"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" \
  configure test \
  --bench-dir "$BENCH_DIR" \
  --site-name "$SITE_NAME" \
  --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-test-second" \
  | tee "$SECOND_ENVIRONMENT_LOG"
tail -n 1 "$SECOND_ENVIRONMENT_LOG" | grep -F '"changed": false'
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" \
  verify test \
  --bench-dir "$BENCH_DIR" \
  --site-name "$SITE_NAME" \
  --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-test-verify"

bench --site "$SITE_NAME" migrate --skip-search-index
bench --site "$SITE_NAME" run-tests --app medops

if grep -Eiq 'password|token|secret' "$BENCH_DIR/logs/environment-audit.jsonl"; then
  echo "Environment audit log contains a forbidden secret marker" >&2
  exit 1
fi

create_profile_site() {
  local profile="$1"
  local site_name="$2"

  bench new-site "$site_name" \
    --db-type "$DB_TYPE" \
    --db-host "$DB_HOST" \
    --db-port "$DB_PORT" \
    --db-root-username "$DB_ROOT_USERNAME" \
    --db-root-password "$DB_ROOT_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD"
  bench --site "$site_name" install-app erpnext
  bench --site "$site_name" install-app hrms
  bench --site "$site_name" install-app medops
  "$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" \
    configure "$profile" \
    --bench-dir "$BENCH_DIR" \
    --site-name "$site_name" \
    --allow-target-override \
    --correlation-id "CI-${GITHUB_RUN_ID:-local}-${profile}-configure"
}

create_profile_site development "$DEVELOPMENT_SITE_NAME"
create_profile_site demo "$DEMO_SITE_NAME"

FIXTURE_EXPORT_LOG="$BENCH_DIR/logs/fixture-export.log"
FIXTURE_MANAGER="$BENCH_DIR/apps/medops/scripts/fixture_manager.py"
"$BENCH_DIR/env/bin/python" "$FIXTURE_MANAGER" validate
"$BENCH_DIR/env/bin/python" "$FIXTURE_MANAGER" plan
"$BENCH_DIR/env/bin/python" "$FIXTURE_MANAGER" export \
  --bench-dir "$BENCH_DIR" \
  --site "$DEVELOPMENT_SITE_NAME" \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-COD-007-fixture-export" \
  --yes \
  | tee "$FIXTURE_EXPORT_LOG"
tail -n 1 "$FIXTURE_EXPORT_LOG" | grep -F '"changed": false'
tail -n 1 "$FIXTURE_EXPORT_LOG" | grep -F '"idempotent": true'
"$BENCH_DIR/env/bin/python" "$FIXTURE_MANAGER" validate
if grep -Eiq 'password|token|secret' "$BENCH_DIR/logs/fixture-export-audit.jsonl"; then
  echo "Fixture export audit log contains a forbidden secret marker" >&2
  exit 1
fi

DEMO_FIRST_LOG="$BENCH_DIR/logs/demo-seed-first.log"
DEMO_SECOND_LOG="$BENCH_DIR/logs/demo-seed-second.log"
bench --site "$DEMO_SITE_NAME" execute medops.setup.demo.setup_synthetic_demo \
  | tee "$DEMO_FIRST_LOG"
bench --site "$DEMO_SITE_NAME" execute medops.setup.demo.setup_synthetic_demo \
  | tee "$DEMO_SECOND_LOG"
tail -n 1 "$DEMO_SECOND_LOG" | grep -F '"changed": false'

"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" \
  verify development \
  --bench-dir "$BENCH_DIR" \
  --site-name "$DEVELOPMENT_SITE_NAME" \
  --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-development-verify"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" \
  verify demo \
  --bench-dir "$BENCH_DIR" \
  --site-name "$DEMO_SITE_NAME" \
  --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-demo-verify"

if grep -Eiq 'password|token|secret' "$BENCH_DIR/logs/environment-audit.jsonl"; then
  echo "Environment audit log contains a forbidden secret marker" >&2
  exit 1
fi

"$BENCH_DIR/env/bin/python" - \
  "$BENCH_DIR" \
  "$SITE_NAME" \
  "$DEVELOPMENT_SITE_NAME" \
  "$DEMO_SITE_NAME" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

bench_dir = Path(sys.argv[1]).resolve()
common_config = json.loads(
	(bench_dir / "sites" / "common_site_config.json").read_text(encoding="utf-8")
)
for site_name in sys.argv[2:]:
	site_config = json.loads(
		(bench_dir / "sites" / site_name / "site_config.json").read_text(encoding="utf-8")
	)
	database_name = site_config["db_name"]
	database_password = site_config["db_password"]
	database_host = site_config.get("db_host") or common_config.get("db_host") or "127.0.0.1"
	database_port = str(site_config.get("db_port") or common_config.get("db_port") or 3306)
	result = subprocess.run(
		[
			"mariadb",
			"--batch",
			"--skip-column-names",
			"--host",
			database_host,
			"--port",
			database_port,
			"--user",
			database_name,
			database_name,
			"--execute",
			"""
			SELECT COUNT(*) FROM `tabError Log`;
			SELECT name, COALESCE(method, ''), creation
			FROM `tabError Log`
			ORDER BY creation, name;
			""",
		],
		env={**os.environ, "MYSQL_PWD": database_password},
		capture_output=True,
		text=True,
		check=True,
	)
	lines = [line for line in result.stdout.splitlines() if line]
	error_log_count = int(lines[0])
	summaries = []
	for line in lines[1:]:
		name, method, creation = line.split("\t", maxsplit=2)
		summaries.append({"name": name, "method": method, "creation": creation})
	print(
		json.dumps(
			{
				"site": site_name,
				"error_log_count": error_log_count,
				"error_log_summaries": summaries,
			},
			sort_keys=True,
		)
	)
	if error_log_count:
		raise SystemExit(f"Fresh CI site {site_name} contains Error Log records")
PY

for app in frappe erpnext hrms medops; do
  if [[ -n "$(git -C "$BENCH_DIR/apps/$app" status --porcelain)" ]]; then
    echo "CI validation changed the $app worktree" >&2
    git -C "$BENCH_DIR/apps/$app" status --short >&2
    exit 1
  fi
done

echo "CI integration validation passed"
