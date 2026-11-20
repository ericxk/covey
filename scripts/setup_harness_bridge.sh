#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  setup_harness_bridge.sh [options]

Install repo skills, report the available Harness runtimes, guide lark-cli auth
for the unified Feishu bot, and register it with the bridge service manager.
Every topic selects its Harness, model, reasoning effort and workspace on the
launch card; there is no per-backend bot.

Options:
  --profile NAME          lark-cli profile of the bot application. Default: harness.
  --workdir DIR           Default workspace. Default: current dir.
  --app-id APP_ID         Expected application. Default: the profile's verified appId.
  --owner OPEN_ID         Owner open_id in this application. Default: the profile's verified user.
  --pairing-file FILE     One-time owner pairing record from bin/covey_pairing.py.
  --harness-config FILE   Per-Harness argv JSON (docs/harnesses.md).
  --state-dir DIR         Bridge state. Default: <data-home>/harness-<APP_ID>.
  --skip-login            Do not run lark-cli (and extension) login flows.
  --no-start              Install/check only; do not register or start the bridge.
  --skip-app-setup        Do not check the Feishu app's scopes, events and menu, nor add
                          what is missing (docs/operations.md 自动补齐飞书应用).
  --non-interactive       Do not prompt; fail if required auth is missing.
  --lark-domains DOMAINS  Domains for lark-cli auth login.
                          Default: im,docs,drive,contact.
  -h, --help              Show this help.

Install the Codex, Claude, dsh, pi or agy CLIs (and what an enabled extension needs)
before setup; Harnesses that are unavailable stay unusable until installed.
USAGE
  if declare -F ext_usage >/dev/null; then ext_usage; fi
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

info() {
  printf '[setup] %s\n' "$*"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(pwd)"
PROFILE="${COVEY_PROFILE:-${LARK_BRIDGE_PROFILE:-harness}}"
APP_ID=""
OWNER=""
PAIRING_FILE=""
HARNESS_CONFIG=""
STATE_DIR=""
SKIP_LOGIN=0
SKIP_BYTEDCLI=0  # privacy: allow 旧调用方还在传 --skip-bytedcli，核心保留这个选项名（规则 10）
NO_START=0
SKIP_APP_SETUP=0
NON_INTERACTIVE=0
LARK_DOMAINS="${HARNESS_LARK_DOMAINS:-im,docs,drive,contact}"
AVAILABLE_HARNESSES=()

# Setup hooks of the enabled extensions (extensions/<package>/scripts/setup_hook.sh): they define ext_usage (text for
# --help), ext_ensure (install what the extension needs) and ext_login. Without extensions none is defined.
if have python3 && [ -d "$ROOT/covey" ]; then
  while IFS= read -r ext_dir; do
    [ -f "$ext_dir/scripts/setup_hook.sh" ] && . "$ext_dir/scripts/setup_hook.sh"
  done < <(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from covey.core import extensions; print("\n".join(map(str, extensions.package_dirs())))' "$ROOT" 2>/dev/null)
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    --app-id) APP_ID="${2:-}"; shift 2 ;;
    --owner) OWNER="${2:-}"; shift 2 ;;
    --pairing-file) PAIRING_FILE="${2:-}"; shift 2 ;;
    --harness-config) HARNESS_CONFIG="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --skip-login) SKIP_LOGIN=1; shift ;;
    --skip-bytedcli) SKIP_BYTEDCLI=1; shift ;;  # privacy: allow 同上，兼容旧选项
    --no-start) NO_START=1; shift ;;
    --skip-app-setup) SKIP_APP_SETUP=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --lark-domains) LARK_DOMAINS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -d "$WORKDIR" ] || die "workdir not found: $WORKDIR"
WORKDIR="$(cd "$WORKDIR" && pwd)"
[ -n "$PROFILE" ] || die "--profile must name the bot's own lark-cli profile"
[ -z "$HARNESS_CONFIG" ] || [ -f "$HARNESS_CONFIG" ] || die "harness config not found: $HARNESS_CONFIG"
[ -z "$PAIRING_FILE" ] || [ -f "$PAIRING_FILE" ] || die "pairing file not found: $PAIRING_FILE"

install_repo_skills() {
  info "installing repo skills"
  "$ROOT/scripts/install_skills.sh"
}

ensure_lark_cli() {
  if have lark-cli; then
    info "lark-cli found: $(command -v lark-cli)"
    return
  fi
  have npm || die "lark-cli missing and npm is not available; install @larksuite/cli then rerun"
  info "installing lark-cli with npm"
  npm install -g @larksuite/cli
  have lark-cli || die "lark-cli install finished but command is still not on PATH"
}

ensure_profile() {
  lark-cli --profile "$PROFILE" config show >/dev/null 2>&1 && return
  [ "$SKIP_LOGIN" -eq 0 ] || die "lark-cli profile $PROFILE is missing; rerun without --skip-login"
  [ "$NON_INTERACTIVE" -eq 0 ] || die "lark-cli profile $PROFILE is missing; create it before --non-interactive"
  info "creating lark-cli profile $PROFILE for the bridge bot"
  lark-cli config init --new --name "$PROFILE" --brand feishu --lang zh
}

lark_status_ok() {
  lark-cli --profile "$PROFILE" auth status --verify --json >/dev/null 2>&1
}

lark_login() {
  if lark_status_ok; then
    info "lark-cli auth ok for profile $PROFILE"
    return
  fi
  [ "$SKIP_LOGIN" -eq 0 ] || die "lark-cli auth missing for profile $PROFILE; rerun without --skip-login"
  [ "$NON_INTERACTIVE" -eq 0 ] || die "lark-cli auth missing for profile $PROFILE; run auth login interactively before --non-interactive"
  info "starting lark-cli auth login for profile $PROFILE"
  lark-cli --profile "$PROFILE" auth login --domain "$LARK_DOMAINS"
  lark_status_ok || die "lark-cli auth verification still failed for profile $PROFILE"
}

# The Feishu app's scopes, events, callback and menu (core/app_manifest.py): what is missing is added in the
# developer console by a browser after one QR scan here (bin/covey_app_setup.py apply), the browser installed
# first when there is none (scripts/install_console_browser.sh).
ensure_app_config() {
  [ "$SKIP_APP_SETUP" -eq 0 ] || return 0
  local rc=0 py
  python3 -B "$ROOT/bin/covey_app_setup.py" check --profile "$PROFILE" ${APP_ID:+--app-id "$APP_ID"} || rc=$?
  [ "$rc" -eq 3 ] || { [ "$rc" -eq 0 ] || warn "Feishu app check failed; see docs/manual-install.md 配置飞书应用"; return 0; }
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    warn "the Feishu app lacks the above; run: python3 -B bin/covey_app_setup.py apply --profile $PROFILE"
    return 0
  fi
  py="${COVEY_PLAYWRIGHT_PYTHON:-${LARK_BRIDGE_PLAYWRIGHT_PYTHON:-$(python3 -B "$ROOT/bin/covey_paths.py" proxy-web)/venv/bin/python}}"
  if [ ! -x "$py" ]; then
    info "installing the browser that configures the Feishu app (Playwright + Chromium, under the data root)"
    py="$("$ROOT/scripts/install_console_browser.sh" | tail -n 1)" || {
      warn "browser install failed; add the above in the developer console by hand (docs/manual-install.md 配置飞书应用)"; return 0; }
  fi
  info "scan the QR below with Feishu on your phone; the browser then adds the above and publishes a version"
  rc=0
  python3 -B "$ROOT/bin/covey_app_setup.py" apply --profile "$PROFILE" ${APP_ID:+--app-id "$APP_ID"} \
    --python "$py" || rc=$?
  [ "$rc" -eq 0 ] || warn "the Feishu app still lacks some of the above; see docs/manual-install.md 配置飞书应用"
}

resolve_app_id() {
  local actual
  actual="$(lark-cli --profile "$PROFILE" auth status --verify --json 2>/dev/null |
    python3 -c 'import json, sys; print(json.load(sys.stdin).get("appId") or "")' 2>/dev/null || true)"
  [ -n "$actual" ] || die "cannot read appId of profile $PROFILE; check lark-cli auth status"
  [ -z "$APP_ID" ] || [ "$APP_ID" = "$actual" ] || die "profile $PROFILE belongs to $actual, not --app-id $APP_ID"
  APP_ID="$actual"
}

detect_harnesses() {
  AVAILABLE_HARNESSES=()
  local cli
  for cli in codex claude dsh pi agy; do
    have "$cli" && AVAILABLE_HARNESSES+=("$cli")
  done
  # An enabled extension's hook adds the Harnesses it brings (ext_detect appends to AVAILABLE_HARNESSES).
  if declare -F ext_detect >/dev/null; then ext_detect; fi
  [ "${#AVAILABLE_HARNESSES[@]}" -gt 0 ] || die "no usable Harness found; install codex, claude, dsh, pi or agy (or prepare an extension's runtime) first"
  local IFS=,
  info "available Harnesses: ${AVAILABLE_HARNESSES[*]}"
  if declare -F ext_report >/dev/null; then ext_report; fi
}

# Fail before replacing installed skills or starting package installation.
[ "${BASH_VERSINFO[0]}" -ge 4 ] || die "Bash 4+ is required"
have python3 || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10+ is required"
have rsync || die "rsync is required; install it before setup"
detect_harnesses
install_repo_skills
ensure_lark_cli
if declare -F ext_ensure >/dev/null; then ext_ensure; fi
if [ "$SKIP_LOGIN" -eq 0 ]; then
  ensure_profile
  lark_login
  ensure_app_config
fi
if declare -F ext_login >/dev/null; then ext_login; fi

if [ "$NO_START" -eq 1 ]; then
  info "installation checks completed; bridge not started. Event subscriptions and model access still need end-to-end verification"
  exit 0
fi

ensure_profile
resolve_app_id
args=(--workdir "$WORKDIR" --profile "$PROFILE" --app-id "$APP_ID")
[ -z "$STATE_DIR" ] || args+=(--state-dir "$STATE_DIR")
[ -z "$OWNER" ] || args+=(--owner "$OWNER")
[ -z "$PAIRING_FILE" ] || args+=(--pairing-file "$PAIRING_FILE")
[ -z "$HARNESS_CONFIG" ] || args+=(--harness-config "$HARNESS_CONFIG")
info "starting the unified bridge for $APP_ID"
"$ROOT/scripts/bootstrap_harness_bridge.sh" "${args[@]}"
info "done"
