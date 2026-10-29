#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${LOCK_FILE:-$ROOT_DIR/resolved_versions.lock.json}"
BENCH_DIR="${BENCH_DIR:-$HOME/frappe-bench-hrp}"
SITE_NAME="${SITE_NAME:-hrp.localhost}"
PYTHON_BIN="${PYTHON_BIN:-python3.10}"
DB_TYPE="${DB_TYPE:-mariadb}"
DB_ROOT_USERNAME="${DB_ROOT_USERNAME:-root}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
DEVELOPER_MODE="${DEVELOPER_MODE:-1}"
KEEP_TEMPORARY_REDIS="${KEEP_TEMPORARY_REDIS:-0}"
REDIS_CACHE_PORT="${REDIS_CACHE_PORT:-13000}"
REDIS_QUEUE_PORT="${REDIS_QUEUE_PORT:-11000}"
SOCKETIO_PORT="${SOCKETIO_PORT:-9000}"
WEBSERVER_PORT="${WEBSERVER_PORT:-8000}"
FILE_WATCHER_PORT="${FILE_WATCHER_PORT:-6787}"

: "${DB_ROOT_PASSWORD:?Set DB_ROOT_PASSWORD in the environment or .env}"
: "${ADMIN_PASSWORD:?Set ADMIN_PASSWORD in the environment or .env}"

lock_value() {
  python3 - "$LOCK_FILE" "$1" "$2" <<'PYLOCK'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload["apps"][sys.argv[2]][sys.argv[3]])
PYLOCK
}

pin_app() {
  local app="$1"
  local commit="$2"
  local app_dir="$BENCH_DIR/apps/$app"
  local remote="origin"
  if ! git -C "$app_dir" remote get-url "$remote" >/dev/null 2>&1; then
	remote="upstream"
  fi
  git -C "$app_dir" fetch --depth 1 "$remote" "$commit"
  # bench get-app may generate lockfiles before the governed commit is pinned.
  git -C "$app_dir" reset --hard HEAD
  git -C "$app_dir" clean -fd
  git -C "$app_dir" checkout --detach "$commit"
  if [[ -n "$(git -C "$app_dir" status --porcelain)" ]]; then
    echo "Pinned app worktree is dirty: $app_dir" >&2
    exit 1
  fi
}

configure_bench_ports() {
  python3 - "$BENCH_DIR" "$REDIS_CACHE_PORT" "$REDIS_QUEUE_PORT" \
    "$SOCKETIO_PORT" "$WEBSERVER_PORT" "$FILE_WATCHER_PORT" <<'PYPORTS'
import json
import re
import sys
from pathlib import Path

bench = Path(sys.argv[1])
cache_port, queue_port, socketio_port, web_port, watcher_port = map(int, sys.argv[2:])
config_path = bench / "sites" / "common_site_config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config.update(
    {
        "redis_cache": f"redis://127.0.0.1:{cache_port}",
        "redis_queue": f"redis://127.0.0.1:{queue_port}",
        "redis_socketio": f"redis://127.0.0.1:{cache_port}",
        "socketio_port": socketio_port,
        "webserver_port": web_port,
        "file_watcher_port": watcher_port,
    }
)
config_path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
for filename, port in (("redis_cache.conf", cache_port), ("redis_queue.conf", queue_port)):
    path = bench / "config" / filename
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^port\s+\d+$", f"port {port}", text)
    path.write_text(text, encoding="utf-8")
PYPORTS
}

stop_temporary_redis() {
  for name in redis_cache redis_queue; do
    local pidfile="$BENCH_DIR/config/pids/$name.pid"
    if [[ -f "$pidfile" ]]; then
      local pid
      pid="$(cat "$pidfile")"
      if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
        kill "$pid"
      fi
    fi
  done
}

start_temporary_redis() {
  command -v redis-server >/dev/null 2>&1 || {
    echo "redis-server is required for Frappe v17 build and migration." >&2
    exit 1
  }
  redis-server "$BENCH_DIR/config/redis_cache.conf" --daemonize yes
  redis-server "$BENCH_DIR/config/redis_queue.conf" --daemonize yes
  if [[ "$KEEP_TEMPORARY_REDIS" != "1" ]]; then
    trap stop_temporary_redis EXIT
  fi
}

FRAPPE_COMMIT="$(lock_value frappe commit)"
ERPNEXT_COMMIT="$(lock_value erpnext commit)"
HRMS_COMMIT="$(lock_value hrms commit)"
FRAPPE_BRANCH="$(lock_value frappe branch)"
ERPNEXT_BRANCH="$(lock_value erpnext branch)"
HRMS_BRANCH="$(lock_value hrms branch)"
INCLUDE_IONE_CORE="${INCLUDE_IONE_CORE:-0}"
case "$INCLUDE_IONE_CORE" in
  0|1) ;;
  *) echo "INCLUDE_IONE_CORE must be 0 or 1" >&2; exit 1 ;;
esac
MEDOPS_BOOTSTRAP_SKIP_SEARCH_INDEX="${MEDOPS_BOOTSTRAP_SKIP_SEARCH_INDEX:-0}"
case "$MEDOPS_BOOTSTRAP_SKIP_SEARCH_INDEX" in
  0|1) ;;
  *) echo "MEDOPS_BOOTSTRAP_SKIP_SEARCH_INDEX must be 0 or 1" >&2; exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it first, then rerun." >&2
  exit 1
fi

command -v bench >/dev/null 2>&1 || {
  echo "Install an approved Bench tool before bootstrap; this script does not fetch official repositories." >&2
  exit 1
}

if [[ -e "$BENCH_DIR" ]]; then
  echo "Bench directory already exists: $BENCH_DIR" >&2
  exit 1
fi

bench init "$BENCH_DIR" \
  --frappe-path https://github.com/jerry317395616/frappe.git \
  --frappe-branch "$FRAPPE_BRANCH" \
  --python "$PYTHON_BIN" \
  --skip-assets \
  --dev

cd "$BENCH_DIR"
configure_bench_ports
start_temporary_redis
pin_app frappe "$FRAPPE_COMMIT"
bench get-app --branch "$ERPNEXT_BRANCH" erpnext https://github.com/jerry317395616/erpnext.git
pin_app erpnext "$ERPNEXT_COMMIT"
bench get-app --branch "$HRMS_BRANCH" hrms https://github.com/jerry317395616/hrms.git
pin_app hrms "$HRMS_COMMIT"
bench setup requirements

# Optional isolated CI compatibility profile. This is the deployed core revision,
# fetched only from the owner's repository; it is not a new medops dependency.
if [[ "$INCLUDE_IONE_CORE" == "1" ]]; then
  IONE_CORE_COMMIT="8c697bafd30aebd418aee18f618b287bbab557f3"
  git clone --no-checkout https://github.com/jerry317395616/ione_core.git apps/ione_core
  git -C apps/ione_core fetch --depth 1 origin "$IONE_CORE_COMMIT"
  git -C apps/ione_core checkout --detach "$IONE_CORE_COMMIT"
  [[ "$(git -C apps/ione_core rev-parse HEAD)" == "$IONE_CORE_COMMIT" ]]
  ./env/bin/python -m pip install -e apps/ione_core
fi

git clone --no-hardlinks "$ROOT_DIR" "$BENCH_DIR/apps/medops"
./env/bin/python -m pip install -e apps/medops
python3 - "$BENCH_DIR/sites/apps.txt" "$INCLUDE_IONE_CORE" <<'PYAPPS'
import sys
from pathlib import Path

path = Path(sys.argv[1])
apps = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
if sys.argv[2] == "1" and "ione_core" not in apps:
    apps.append("ione_core")
if "medops" not in apps:
    apps.append("medops")
path.write_text("\n".join(apps) + "\n", encoding="utf-8")
PYAPPS

bash "$ROOT_DIR/scripts/lock_versions.sh" "$BENCH_DIR" "$LOCK_FILE"

bench new-site "$SITE_NAME" \
  --db-type "$DB_TYPE" \
  --db-host "$DB_HOST" \
  --db-port "$DB_PORT" \
  --db-root-username "$DB_ROOT_USERNAME" \
  --db-root-password "$DB_ROOT_PASSWORD" \
  --admin-password "$ADMIN_PASSWORD" \
  --set-default

bench --site "$SITE_NAME" install-app erpnext
bench --site "$SITE_NAME" install-app hrms
if [[ "$INCLUDE_IONE_CORE" == "1" ]]; then
  bench --site "$SITE_NAME" install-app ione_core
fi
bench --site "$SITE_NAME" install-app medops
bench set-config -g developer_mode "$DEVELOPER_MODE"
if [[ "$MEDOPS_BOOTSTRAP_SKIP_SEARCH_INDEX" == "1" ]]; then
  bench --site "$SITE_NAME" migrate --skip-search-index
else
  bench --site "$SITE_NAME" migrate
fi
# Fresh Bench initialization skips assets. Build every installed app so the
# native website/login/Desk dependencies exist before verifying the custom UI.
if [[ "$INCLUDE_IONE_CORE" == "1" ]]; then
  ./env/bin/python "$ROOT_DIR/scripts/core_build_integrity.py" prepare --app "$BENCH_DIR/apps/ione_core"
fi
bench build
if [[ "$INCLUDE_IONE_CORE" == "1" ]]; then
  ./env/bin/python "$ROOT_DIR/scripts/core_build_integrity.py" seal \
    --app "$BENCH_DIR/apps/ione_core" --proof "$BENCH_DIR/.medops-core-build-integrity.json"
fi

echo "Ready. Run: cd '$BENCH_DIR' && bench start"
