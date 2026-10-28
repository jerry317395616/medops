#!/usr/bin/env bash
set -euo pipefail

# Manual feedback only: this does not replace the full required CI workflow.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="${BENCH_DIR:?Set BENCH_DIR to a new CI workspace path}"
SITE_NAME="${SITE_NAME:-hrp-focused.localhost}"
REDIS_CACHE_PORT="${REDIS_CACHE_PORT:-13000}"
REDIS_QUEUE_PORT="${REDIS_QUEUE_PORT:-11000}"
if [[ "${GITHUB_ACTIONS:-}" != "true" || -z "${RUNNER_TEMP:-}" ]]; then
  echo "Focused integration only runs on a GitHub Actions runner" >&2
  exit 1
fi
runner_temp_real="$(realpath -e -- "$RUNNER_TEMP")"
bench_dir_real="$(realpath -m -- "$BENCH_DIR")"
case "$bench_dir_real" in
  "$runner_temp_real"/*) ;;
  *) echo "Focused Bench must be inside the runner temporary directory" >&2; exit 1 ;;
esac
BENCH_DIR="$bench_dir_real"
export BENCH_DIR
export PATH="$HOME/.local/bin:$PATH"
export KEEP_TEMPORARY_REDIS=1
export MEDOPS_BOOTSTRAP_SKIP_SEARCH_INDEX=1

if [[ -e "$BENCH_DIR" ]]; then
  echo "Focused CI Bench directory already exists" >&2
  exit 1
fi
for port in "$REDIS_CACHE_PORT" "$REDIS_QUEUE_PORT"; do
  if redis-cli -p "$port" ping >/dev/null 2>&1; then
    echo "Focused CI Redis port is already in use" >&2
    exit 1
  fi
done
stop_redis() {
  redis-cli -p "$REDIS_CACHE_PORT" shutdown nosave >/dev/null 2>&1 || true
  redis-cli -p "$REDIS_QUEUE_PORT" shutdown nosave >/dev/null 2>&1 || true
}
trap stop_redis EXIT

bash "$ROOT_DIR/scripts/bootstrap_latest_develop.sh"
cd "$BENCH_DIR"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/version_lock.py" --bench "$BENCH_DIR"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" configure test \
  --bench-dir "$BENCH_DIR" --site-name "$SITE_NAME" --allow-target-override \
  --correlation-id "FOCUSED-${GITHUB_RUN_ID:-local}-configure"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" verify test \
  --bench-dir "$BENCH_DIR" --site-name "$SITE_NAME" --allow-target-override \
  --correlation-id "FOCUSED-${GITHUB_RUN_ID:-local}-verify"
bench --site "$SITE_NAME" migrate --skip-search-index

lineage_log="$BENCH_DIR/logs/focused-renewed-lineage.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_authorization_observation 2>&1 | tee "$lineage_log"
if ! grep -Eq 'Ran 12 tests? in [0-9.]+s' "$lineage_log"; then echo "Missing renewal observation checks" >&2; exit 1; fi
for spec in \
  "medops.tests.test_budget_material_request_source:test_real_submitted_purchase_order_is_linked_to_material_request" \
  "medops.tests.test_budget_release_execution:test_private_initial_reservation_writes_and_rolls_back_atomically" \
  "medops.tests.test_budget_release_execution:test_private_renewal_appends_replays_and_rolls_back_atomically"; do
  module="${spec%%:*}"
  testname="${spec#*:}"
  testlog="$BENCH_DIR/logs/focused-${testname}.log"
  bench --site "$SITE_NAME" run-tests --app medops --module "$module" --test "$testname" 2>&1 | tee "$testlog"
  if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$testlog"; then echo "Missing focused test: $testname" >&2; exit 1; fi
done

"$BENCH_DIR/env/bin/python" - "$BENCH_DIR" "$SITE_NAME" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

bench_dir = Path(sys.argv[1]).resolve()
site = json.loads((bench_dir / "sites" / sys.argv[2] / "site_config.json").read_text(encoding="utf-8"))
common = json.loads((bench_dir / "sites" / "common_site_config.json").read_text(encoding="utf-8"))
result = subprocess.run(
    ["mariadb", "--batch", "--skip-column-names", "--host", site.get("db_host") or common.get("db_host") or "127.0.0.1",
     "--port", str(site.get("db_port") or common.get("db_port") or 3306), "--user", site["db_name"],
     site["db_name"], "--execute", "SELECT COUNT(*) FROM `tabError Log`"],
    env={**os.environ, "MYSQL_PWD": site["db_password"]}, capture_output=True, text=True, check=True,
)
count = int(result.stdout.strip())
print(json.dumps({"site": sys.argv[2], "error_log_count": count, "scope": "focused_budget_only"}))
if count:
    raise SystemExit("Focused CI site contains Error Log records")
PY
