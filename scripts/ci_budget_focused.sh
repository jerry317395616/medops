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


#!/usr/bin/env bash
set -euo pipefail

# ROOT_DIR remains the absolute path resolved before entering the Bench.
BENCH_DIR="${BENCH_DIR:?Set BENCH_DIR to an existing isolated CI Bench}"
PROBE_SITE="hrp-reservation-concurrency.localhost"
if [[ "${GITHUB_ACTIONS:-}" != "true" || -z "${RUNNER_TEMP:-}" ]]; then
  echo "Reservation concurrency probe only runs on a disposable GitHub runner" >&2
  exit 1
fi
runner_real="$(realpath -e -- "$RUNNER_TEMP")"
bench_real="$(realpath -e -- "$BENCH_DIR")"
case "$bench_real" in
  "$runner_real"/*) ;;
  *) echo "Probe Bench must be inside runner temporary storage" >&2; exit 1 ;;
esac
if [[ -e "$bench_real/sites/$PROBE_SITE" ]]; then
  echo "Disposable concurrency site already exists" >&2
  exit 1
fi
: "${DB_ROOT_PASSWORD:?Set DB_ROOT_PASSWORD}"
: "${ADMIN_PASSWORD:?Set ADMIN_PASSWORD}"
export PATH="$HOME/.local/bin:$PATH"
cd "$bench_real"
bench new-site "$PROBE_SITE" \
  --db-type "${DB_TYPE:-mariadb}" --db-host "${DB_HOST:-127.0.0.1}" --db-port "${DB_PORT:-3306}" \
  --db-root-username "${DB_ROOT_USERNAME:-root}" --db-root-password "$DB_ROOT_PASSWORD" \
  --admin-password "$ADMIN_PASSWORD"
bench --site "$PROBE_SITE" install-app erpnext
bench --site "$PROBE_SITE" install-app hrms
if [[ "${INCLUDE_IONE_CORE:-0}" == "1" ]]; then
  bench --site "$PROBE_SITE" install-app ione_core
fi
bench --site "$PROBE_SITE" install-app medops
"$bench_real/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" configure test \
  --bench-dir "$bench_real" --site-name "$PROBE_SITE" --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-reservation-concurrency"
"$bench_real/env/bin/python" "$ROOT_DIR/scripts/environment_manager.py" verify test \
  --bench-dir "$bench_real" --site-name "$PROBE_SITE" --allow-target-override \
  --correlation-id "CI-${GITHUB_RUN_ID:-local}-reservation-concurrency-verify"
bench --site "$PROBE_SITE" migrate --skip-search-index
"$bench_real/env/bin/python" - "$bench_real/sites/$PROBE_SITE/site_config.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
config = json.loads(path.read_text(encoding="utf-8"))
config.update(
    medops_ci_disposable_reservation_site=True,
    medops_internal_reservation_write_enabled=True,
    medops_internal_reservation_renewal_enabled=True,
    medops_internal_renewed_reservation_write_enabled=True,
    medops_internal_reservation_release_write_enabled=True,
    medops_reservation_confirmation_enabled=True,
    medops_reservation_operations_enabled=True,
)
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

uv pip install --python "$BENCH_DIR/env/bin/python" playwright==1.64.1
"$BENCH_DIR/env/bin/python" -m playwright install --with-deps chromium
cd "$BENCH_DIR/sites"
MEDOPS_CI_RESERVATION_CONCURRENCY=1 "$BENCH_DIR/env/bin/python" "$ROOT_DIR/scripts/ci_native_desk_route_trace.py"
