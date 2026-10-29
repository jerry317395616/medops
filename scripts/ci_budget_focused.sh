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


if [[ "${INCLUDE_IONE_CORE:-0}" == "1" ]]; then
  read -r CORE_BUILD_DIGEST _ < <(sha256sum "$BENCH_DIR/.medops-core-build-integrity.json")
fi
cd "$BENCH_DIR/sites"
cd "$ROOT_DIR"
pure_log="$BENCH_DIR/logs/commitment-provenance-pure.log"
"$BENCH_DIR/env/bin/python" -m unittest tests.test_reservation_commitment 2>&1 | tee "$pure_log"
if ! grep -Eq 'Ran 8 tests? in [0-9.]+s' "$pure_log" || grep -Eq 'OK \(skipped=|FAILED \(' "$pure_log"; then exit 1; fi
cd "$BENCH_DIR/sites"
source_log="$BENCH_DIR/logs/commitment-order-source.log"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/ci_focus_without_locals.py" frappe --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_purchase_order_source 2>&1 | tee "$source_log"
if ! grep -Eq 'Ran 24 tests? in [0-9.]+s' "$source_log" || grep -Eq 'OK \(skipped=|FAILED \(' "$source_log"; then exit 1; fi
actual_log="$BENCH_DIR/logs/commitment-actual-order-source.log"
"$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/ci_focus_without_locals.py" frappe --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_request_source --test test_real_submitted_purchase_order_is_linked_to_material_request 2>&1 | tee "$actual_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$actual_log" || grep -Eq 'OK \(skipped=|FAILED \(' "$actual_log"; then exit 1; fi
"$BENCH_DIR/env/bin/python" - "$BENCH_DIR" "$SITE_NAME" <<'PYLOG'
import json,os,subprocess,sys
from pathlib import Path
bench=Path(sys.argv[1]);site=json.loads((bench/'sites'/sys.argv[2]/'site_config.json').read_text());common=json.loads((bench/'sites/common_site_config.json').read_text())
result=subprocess.run(['mariadb','--batch','--skip-column-names','--host',site.get('db_host') or common.get('db_host') or '127.0.0.1','--port',str(site.get('db_port') or common.get('db_port') or 3306),'--user',site['db_name'],site['db_name'],'--execute','SELECT COUNT(*) FROM `tabError Log`'],env={**os.environ,'MYSQL_PWD':site['db_password']},capture_output=True,text=True,check=True)
count=int(result.stdout.strip());print(json.dumps({'site':sys.argv[2],'error_log_count':count,'scope':'PO provenance24 and actual submitted MR/PO1 only'}))
if count:raise SystemExit('Focused metadata site contains Error Log')
PYLOG
if [[ "${INCLUDE_IONE_CORE:-0}" == "1" ]]; then
  "$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/core_build_integrity.py" verify --app "$BENCH_DIR/apps/ione_core" --proof "$BENCH_DIR/.medops-core-build-integrity.json" --digest "$CORE_BUILD_DIGEST"
fi
for app in frappe erpnext hrms medops; do
  if [[ -n "$(git -C "$BENCH_DIR/apps/$app" status --porcelain)" ]]; then echo "Focused tests changed $app source" >&2; exit 1; fi
done
