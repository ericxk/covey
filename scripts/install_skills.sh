#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_DEST="${CODEX_HOME:-$HOME/.codex}/skills"
CLAUDE_DEST="${CLAUDE_HOME:-$HOME/.claude}/skills"
# Retired skills: the per-backend bot skills replaced by harness-bridge-onboarding,
# and cr-mr-review-loop. Never delete them automatically: an installed copy may
# have local changes.
RETIRED_SKILLS=(lark-codex-bridge lark-claude-bridge lark-neeko-bridge lark-maso-bridge cr-mr-review-loop)  # privacy: allow 已下线的旧 Skill 名，安装时清理

install_to() {
  local dest="$1"
  mkdir -p "$dest"

  for skill in "$ROOT"/skills/*; do
    [ -d "$skill" ] || continue
    name="$(basename "$skill")"
    tmp="$dest/$name.tmp"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    rsync -a --delete --exclude '__pycache__' "$skill/" "$tmp/"
    rm -rf "$dest/$name"
    mv "$tmp" "$dest/$name"
    printf 'installed %s -> %s\n' "$name" "$dest/$name"
  done
  for name in "${RETIRED_SKILLS[@]}"; do
    if [ -e "$dest/$name" ]; then
      printf 'warning: retired skill still installed: %s; review and remove it manually\n' "$dest/$name" >&2
    fi
  done
}

install_to "$CODEX_DEST"
install_to "$CLAUDE_DEST"
