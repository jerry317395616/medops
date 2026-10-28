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

cd "$BENCH_DIR/sites"
for case_name in TestBudgetReservationRecovery TestBudgetReservationOperationsBoundary TestBudgetReservationOperationsHTTP; do
  native_log="$BENCH_DIR/logs/human-${case_name}.log"
  expected=3
  module_name=medops.tests.test_budget_reservation_operations_api
  if [[ "$case_name" == "TestBudgetReservationRecovery" ]]; then expected=4; module_name=medops.tests.test_budget_reservation_recovery; fi
  if [[ "$case_name" == "TestBudgetReservationOperationsBoundary" ]]; then expected=4; fi
  "$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/ci_focus_without_locals.py" frappe --site "$SITE_NAME" run-tests --app medops --module "$module_name" --case "$case_name" 2>&1 | tee "$native_log"
  if ! grep -Eq "Ran $expected tests? in [0-9.]+s" "$native_log" || grep -Eq 'OK \(skipped=|FAILED \(' "$native_log"; then
    echo "Missing human reservation native boundary tests" >&2
    exit 1
  fi
done
cd "$ROOT_DIR"
npm run test:frontend

bash "$ROOT_DIR/scripts/ci_reservation_concurrency.sh"

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

uv pip install --python "$BENCH_DIR/env/bin/python" playwright==1.64.1
"$BENCH_DIR/env/bin/python" -m playwright install --with-deps chromium
cd "$BENCH_DIR/sites"
MEDOPS_CI_RESERVATION_CONCURRENCY=1 "$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/ci_native_reservation_desk_probe.py"
