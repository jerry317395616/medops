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
case "$BENCH_DIR" in
  "$RUNNER_TEMP"/*) ;;
  *) echo "Focused Bench must be inside the runner temporary directory" >&2; exit 1 ;;
esac
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

test_log="$BENCH_DIR/logs/focused-budget-tests.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_release_execution \
  --test test_persisted_authorized_release_guard_checks_original_units_without_posting \
  --test test_persisted_release_guard_poisoned_when_actor_check_is_revoked \
  --test test_persisted_release_guard_detects_role_revocation_from_another_transaction \
  --test test_persisted_reversal_guard_verifies_the_complete_original_batch \
  --test test_guarded_release_inventory_reconciles_posted_batches_and_physical_entries \
  2>&1 | tee "$test_log"
if ! grep -Eq 'Ran 5 tests? in [0-9.]+s' "$test_log"; then
  echo "Focused CI did not execute the expected five tests" >&2
  exit 1
fi

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
