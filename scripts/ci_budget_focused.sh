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

release_log="$BENCH_DIR/logs/focused-release-jobs.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_release_execution \
  --test test_actual_framework_jobs_post_and_completely_reverse_exact_original_grain 2>&1 | tee "$release_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$release_log"; then echo "Missing release regression" >&2; exit 1; fi
real_release_confirmation_log="$BENCH_DIR/logs/focused-real-release-confirmation.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_release_execution \
  --test test_real_posted_release_and_purchase_create_confirmation_evidence 2>&1 | tee "$real_release_confirmation_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$real_release_confirmation_log"; then echo "Missing real release confirmation check" >&2; exit 1; fi

usage_log="$BENCH_DIR/logs/focused-usage-observation.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_usage_observation \
  --case TestBudgetUsageObservation 2>&1 | tee "$usage_log"
if ! grep -Eq 'Ran 32 tests? in [0-9.]+s' "$usage_log"; then echo "Missing scope observation tests" >&2; exit 1; fi
reversal_log="$BENCH_DIR/logs/focused-reversal-usage.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_release_usage_guard \
  --case TestBudgetReleaseUsageGuard 2>&1 | tee "$reversal_log"
if ! grep -Eq 'Ran 6 tests? in [0-9.]+s' "$reversal_log"; then echo "Missing reversal checks" >&2; exit 1; fi
source_log="$BENCH_DIR/logs/focused-source-observation.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_reservation_observation \
  --case TestBudgetReservationObservation 2>&1 | tee "$source_log"
if ! grep -Eq 'Ran 11 tests? in [0-9.]+s' "$source_log"; then echo "Missing source observations" >&2; exit 1; fi

multi_log="$BENCH_DIR/logs/focused-multi-source-guards.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_multi_source_guards \
  --case TestBudgetMultiSourceGuards 2>&1 | tee "$multi_log"
if ! grep -Eq 'Ran 7 tests? in [0-9.]+s' "$multi_log"; then echo "Missing multi-source guard checks" >&2; exit 1; fi
real_multi_log="$BENCH_DIR/logs/focused-real-multi-source.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_request_source \
  --test test_two_real_purchase_sources_are_locked_before_shared_budget_guard 2>&1 | tee "$real_multi_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$real_multi_log"; then echo "Missing real multi-source check" >&2; exit 1; fi
guard_log="$BENCH_DIR/logs/focused-generic-guard.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_control_guards \
  --case TestBudgetControlGuards 2>&1 | tee "$guard_log"
if ! grep -Eq 'Ran 9 tests? in [0-9.]+s' "$guard_log"; then echo "Missing generic guard checks" >&2; exit 1; fi

preview_log="$BENCH_DIR/logs/focused-usage-preview.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_usage_preview \
  --case TestBudgetUsagePreview 2>&1 | tee "$preview_log"
if ! grep -Eq 'Ran 14 tests? in [0-9.]+s' "$preview_log"; then echo "Missing usage preview checks" >&2; exit 1; fi

native_preview_log="$BENCH_DIR/logs/focused-native-usage-preview.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_request_source \
  --test test_real_purchase_source_flows_through_empty_usage_inspection 2>&1 | tee "$native_preview_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$native_preview_log"; then echo "Missing native source preview check" >&2; exit 1; fi

storage_log="$BENCH_DIR/logs/focused-reservation-storage.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_reservation_storage \
  --case TestBudgetReservationStorage 2>&1 | tee "$storage_log"
if ! grep -Eq 'Ran 4 tests? in [0-9.]+s' "$storage_log"; then echo "Missing migrated storage checks" >&2; exit 1; fi

po_source_log="$BENCH_DIR/logs/focused-purchase-order-source.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_purchase_order_source \
  --case TestBudgetPurchaseOrderSource 2>&1 | tee "$po_source_log"
if ! grep -Eq 'Ran 18 tests? in [0-9.]+s' "$po_source_log"; then echo "Missing purchase-order source checks" >&2; exit 1; fi

reservation_confirmation_log="$BENCH_DIR/logs/focused-native-reservation-confirmation.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_request_source \
  --test test_private_reservation_confirmation_persists_exact_append_only_evidence \
  --test test_reservation_preview_uses_real_session_source_and_budget_guard \
  --test test_authority_refresh_preserves_web_session_and_request_form \
  --test test_reservation_domain_services_replay_and_protect_confirmation_proof \
  --test test_authority_refresh_allows_verified_background_actor_switch 2>&1 | tee "$reservation_confirmation_log"
if ! grep -Eq 'Ran 5 tests? in [0-9.]+s' "$reservation_confirmation_log"; then echo "Missing native reservation confirmation check" >&2; exit 1; fi

native_po_log="$BENCH_DIR/logs/focused-native-purchase-order.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_request_source \
  --test test_real_submitted_purchase_order_is_linked_to_material_request 2>&1 | tee "$native_po_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$native_po_log"; then echo "Missing native purchase order check" >&2; exit 1; fi

po_discovery_log="$BENCH_DIR/logs/focused-purchase-order-discovery.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_purchase_order_source \
  --case TestBudgetPurchaseOrderDiscovery 2>&1 | tee "$po_discovery_log"
if ! grep -Eq 'Ran 6 tests? in [0-9.]+s' "$po_discovery_log"; then echo "Missing purchase-order discovery checks" >&2; exit 1; fi

historical_log="$BENCH_DIR/logs/focused-material-snapshot-contract.log"
(cd "$ROOT_DIR" && "$BENCH_DIR/env/bin/python" -m unittest tests.test_material_source_snapshot) 2>&1 | tee "$historical_log"
if ! grep -Eq 'Ran 14 tests? in [0-9.]+s' "$historical_log"; then echo "Missing historical source contract checks" >&2; exit 1; fi

reservation_api_log="$BENCH_DIR/logs/focused-reservation-api-boundary.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_reservation_api \
  --case TestBudgetReservationAPI 2>&1 | tee "$reservation_api_log"
if ! grep -Eq 'Ran 5 tests? in [0-9.]+s' "$reservation_api_log"; then echo "Missing reservation API boundary checks" >&2; exit 1; fi

reservation_http_log="$BENCH_DIR/logs/focused-reservation-http.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_reservation_api \
  --case TestBudgetReservationHTTP 2>&1 | tee "$reservation_http_log"
if ! grep -Eq 'Ran 3 tests? in [0-9.]+s' "$reservation_http_log"; then echo "Missing reservation HTTP checks" >&2; exit 1; fi

reservation_durable_log="$BENCH_DIR/logs/focused-reservation-durable-http.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_reservation_api \
  --case TestBudgetReservationDurableHTTP 2>&1 | tee "$reservation_durable_log"
if ! grep -Eq 'Ran 1 tests? in [0-9.]+s' "$reservation_durable_log"; then echo "Missing reservation durable HTTP check" >&2; exit 1; fi

reservation_binding_log="$BENCH_DIR/logs/focused-reservation-binding.log"
bench --site "$SITE_NAME" run-tests --app medops --module medops.tests.test_budget_material_authorization_observation \
  --case TestBudgetMaterialAuthorizationObservation 2>&1 | tee "$reservation_binding_log"
if ! grep -Eq 'Ran 6 tests? in [0-9.]+s' "$reservation_binding_log"; then echo "Missing guarded authorization binding checks" >&2; exit 1; fi

reservation_binding_pure_log="$BENCH_DIR/logs/focused-reservation-binding-pure.log"
(cd "$ROOT_DIR" && "$BENCH_DIR/env/bin/python" -m unittest tests.test_authorized_reservation_projection) 2>&1 | tee "$reservation_binding_pure_log"
if ! grep -Eq 'Ran 5 tests? in [0-9.]+s' "$reservation_binding_pure_log"; then echo "Missing authorization binding contract checks" >&2; exit 1; fi

scope_observation_log="$BENCH_DIR/logs/focused-budget-scope-observation.log"
(cd "$ROOT_DIR" && "$BENCH_DIR/env/bin/python" -m unittest tests.test_budget_scope_observation) 2>&1 | tee "$scope_observation_log"
if ! grep -Eq 'Ran 8 tests? in [0-9.]+s' "$scope_observation_log"; then echo "Missing budget scope observation checks" >&2; exit 1; fi

authorization_log="$BENCH_DIR/logs/focused-reservation-authorization-contract.log"
(cd "$ROOT_DIR" && "$BENCH_DIR/env/bin/python" -m unittest tests.test_reservation_authorization) 2>&1 | tee "$authorization_log"
if ! grep -Eq 'Ran 24 tests? in [0-9.]+s' "$authorization_log"; then echo "Missing reservation authorization contract checks" >&2; exit 1; fi

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
