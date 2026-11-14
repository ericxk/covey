#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR=""
PROFILE=""
APP_ID=""
STATE_DIR=""
EXTRA=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --workdir) WORKDIR="${2:?}"; shift 2 ;;
    --profile) PROFILE="${2:?}"; shift 2 ;;
    --app-id) APP_ID="${2:?}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:?}"; shift 2 ;;
    --help|-h)
      printf '%s\n' 'Usage: bootstrap_harness_bridge.sh --workdir DIR --profile PROFILE --app-id APP_ID [--state-dir DIR] [bridge options]'
      printf '%s\n' 'Bridge options: --owner OPEN_ID or --pairing-file FILE; --harness-config FILE; Codex model/transport flags.'
      printf '%s\n' 'Codex sandbox defaults to danger-full-access; --sandbox can restrict it.'
      printf '%s\n' 'Persists service configuration. Boot registration is installed separately with covey_service_install.py.'
      exit 0 ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done
[ -n "$PROFILE" ] && [ -n "$APP_ID" ] && [ -d "$WORKDIR" ] || { printf 'workdir, profile and app-id are required\n' >&2; exit 2; }
# The manifest keeps this shell's LARK_* settings. A permission variable the backends no longer read, set to a
# value that restricted them, would register a service looser than asked (core/ignored_env.py): refused.
python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from covey.core.ignored_env import loosened
lines = loosened()
for line in lines: print("refused: " + line, file=sys.stderr)
sys.exit(2 if lines else 0)' "$ROOT" || exit 2
if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$(python3 -B "$ROOT/bin/covey_paths.py" "harness-$APP_ID" --new)"
fi
python3 -B "$ROOT/bin/covey_service.py" launch \
  --state-dir "$STATE_DIR" --workdir "$WORKDIR" --profile "$PROFILE" \
  -- "$(command -v python3)" -B "$ROOT/bin/covey_bridge.py" --harness unified \
  --state-dir "$STATE_DIR" --workdir "$WORKDIR" --profile "$PROFILE" --app-id "$APP_ID" \
  --sandbox danger-full-access "${EXTRA[@]}"
python3 -B "$ROOT/bin/covey_service.py" status --state-dir "$STATE_DIR"
