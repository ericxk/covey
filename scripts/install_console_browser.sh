#!/usr/bin/env bash
# Install the browser the Bridge drives the Feishu developer console with (docs/operations.md 自动补齐飞书应用):
# a Python venv with Playwright and its Chromium, under the data root, then one headless start to prove it runs.
#
#   install_console_browser.sh [--venv DIR]
#
# DIR defaults to <data home>/proxy-web/venv (the acceptance page's interpreter too; an existing one is reused).
# Chromium goes to PLAYWRIGHT_BROWSERS_PATH (default ~/.cache/ms-playwright). Installing Chromium's system
# libraries needs root (python -m playwright install-deps chromium); without them the start fails and says which.
# Prints the interpreter's path last; set COVEY_PLAYWRIGHT_PYTHON to it when DIR is not the default.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLAYWRIGHT_VERSION="1.56.0"
VENV=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --venv) VENV="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$VENV" ] || VENV="$(python3 -B "$ROOT/bin/covey_paths.py" proxy-web)/venv"
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV"
fi
if ! "$PY" -c 'import playwright' >/dev/null 2>&1; then
  PIP_CACHE_DIR="$(dirname "$VENV")/pip-cache" "$PY" -m pip install --quiet "playwright==$PLAYWRIGHT_VERSION"
fi
"$PY" -m playwright install chromium
"$PY" - <<'PYCODE'
import sys
from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser.new_page().goto("about:blank")
        browser.close()
except Exception as exc:  # noqa: BLE001 - said in one line
    print(f"Chromium does not start: {' '.join(str(exc).split())[:600]}", file=sys.stderr)
    print("Missing system libraries are installed (as root) by: python -m playwright install-deps chromium",
          file=sys.stderr)
    sys.exit(1)
PYCODE
echo "$PY"
