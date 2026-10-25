#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${LOCK_FILE:-$ROOT_DIR/resolved_versions.lock.json}"
BENCH_DIR="${BENCH_DIR:?Set BENCH_DIR}"
SITE_NAME="${SITE_NAME:?Set SITE_NAME}"

cd "$BENCH_DIR"
for app in frappe erpnext hrms; do
  [[ -d "apps/$app" ]] || { echo "Missing dependency app: $app" >&2; exit 1; }
done
bash "$ROOT_DIR/scripts/lock_versions.sh" "$BENCH_DIR" "$LOCK_FILE"

if [[ -e apps/medops ]]; then
  echo "apps/medops already exists; refusing to overwrite" >&2
  exit 1
fi
git clone --no-hardlinks "$ROOT_DIR" apps/medops
./env/bin/python -m pip install -e apps/medops
python3 - "$BENCH_DIR/sites/apps.txt" <<'PYAPPS'
import sys
from pathlib import Path

path = Path(sys.argv[1])
apps = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
if "medops" not in apps:
    apps.append("medops")
path.write_text("\n".join(apps) + "\n", encoding="utf-8")
PYAPPS
bench --site "$SITE_NAME" install-app medops
bench --site "$SITE_NAME" migrate
bench build --app medops
