#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
    medops_internal_commitment_confirmation_enabled=True,
    medops_internal_commitment_write_enabled=True,
)
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY
probe_log="$bench_real/logs/reservation-concurrency-probe.log"
MEDOPS_CI_RESERVATION_CONCURRENCY=1 bench --site "$PROBE_SITE" run-tests --app medops \
  --module medops.tests.reservation_concurrency_probe 2>&1 | tee "$probe_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$probe_log" || grep -Eq 'OK \(skipped=|FAILED \(' "$probe_log"; then
  echo "Missing committed-data reservation concurrency probe" >&2
  exit 1
fi
"$bench_real/env/bin/python" - "$bench_real" "$PROBE_SITE" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

bench = Path(sys.argv[1]).resolve()
site = json.loads((bench / "sites" / sys.argv[2] / "site_config.json").read_text(encoding="utf-8"))
common = json.loads((bench / "sites" / "common_site_config.json").read_text(encoding="utf-8"))
result = subprocess.run(
    ["mariadb", "--batch", "--skip-column-names", "--host", site.get("db_host") or common.get("db_host") or "127.0.0.1",
     "--port", str(site.get("db_port") or common.get("db_port") or 3306), "--user", site["db_name"],
     site["db_name"], "--execute", "SELECT COUNT(*) FROM `tabError Log`"],
    env={**os.environ, "MYSQL_PWD": site["db_password"]}, capture_output=True, text=True, check=True,
)
count = int(result.stdout.strip())
print(json.dumps({"site": sys.argv[2], "error_log_count": count, "scope": "disposable_reservation_concurrency"}))
if count:
    raise SystemExit("Concurrency probe site contains Error Log records")
PY
