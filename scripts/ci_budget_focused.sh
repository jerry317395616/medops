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

test_log="$BENCH_DIR/logs/focused-release-inventory.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_release_execution \
  --test test_guarded_release_inventory_reconciles_posted_batches_and_physical_entries \
  --test test_guarded_purchase_match_reads_real_posted_release_without_financial_writes \
  2>&1 | tee "$test_log"
if ! grep -Eq 'Ran 2 tests? in [0-9.]+s' "$test_log"; then
  echo "Focused CI did not execute both release and purchase tests" >&2
  exit 1
fi

candidate_log="$BENCH_DIR/logs/focused-material-candidates.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_candidates \
  --case TestBudgetMaterialCandidates 2>&1 | tee "$candidate_log"
if ! grep -Eq 'Ran 6 tests? in [0-9.]+s' "$candidate_log"; then
  echo "Focused CI did not execute six guarded candidate tests" >&2
  exit 1
fi

source_log="$BENCH_DIR/logs/focused-material-source.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_request_source \
  --case TestBudgetMaterialRequestSource 2>&1 | tee "$source_log"
if ! grep -Eq 'Ran 3 tests? in [0-9.]+s' "$source_log"; then
  echo "Focused CI did not execute three source tests" >&2
  exit 1
fi

observation_log="$BENCH_DIR/logs/focused-reservation-observation.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_reservation_observation \
  --case TestBudgetReservationObservation 2>&1 | tee "$observation_log"
if ! grep -Eq 'Ran 9 tests? in [0-9.]+s' "$observation_log"; then
  echo "Focused CI did not execute nine reservation observation tests" >&2
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
