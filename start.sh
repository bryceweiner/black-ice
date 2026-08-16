#!/usr/bin/env bash
#
# Start Black Ice: model server, dashboard, API, and voice — one process, one
# Ctrl-C. Everything is served from http://HOST:PORT.
#
#   ./start.sh              dashboard + API + voice
#   ./start.sh --no-voice   dashboard + API only
#   ./start.sh --dev        Vite dev server on :5173 with hot reload
#   ./start.sh --rebuild    force a dashboard rebuild first
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VOICE=1; DEV=0; REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --no-voice) VOICE=0 ;;
    --dev)      DEV=1 ;;
    --rebuild)  REBUILD=1 ;;
    -h|--help)  sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null || die "uv is not installed (https://docs.astral.sh/uv/)"

# --- configuration ----------------------------------------------------------

[[ -f .env ]] || {
  warn ".env missing; creating from .env.example"
  cp .env.example .env
  warn "set ADMIN_PASSWORD_HASH:  uv run blackice hash-password"
}

# Read what we need without sourcing .env (values may contain spaces or #).
getenv() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
HOST=$(getenv HOST); HOST=${HOST:-0.0.0.0}
PORT=$(getenv PORT); PORT=${PORT:-8080}
[[ -n "$(getenv ADMIN_PASSWORD_HASH)" ]] || \
  warn "ADMIN_PASSWORD_HASH is empty — login will fail. Run: uv run blackice hash-password"

# `uv sync` prunes anything not in the lockfile, so the extras must be named
# here or it will happily uninstall voice2, kokoro-memory and piper.
say "syncing python dependencies"
uv sync --quiet --extra all

# Local plugins are editable installs and get pruned by the same rule.
for plugin in plugins/*/; do
  [[ -f "$plugin/pyproject.toml" ]] || continue
  name=$(basename "$plugin")
  uv run python -c "
import sys
from importlib.metadata import entry_points
sys.exit(0 if any(e.dist and e.dist.name == '$name'
                  for e in entry_points(group='blackice.plugins')) else 1)
" 2>/dev/null || { say "installing plugin $name"; uv pip install -q -e "$plugin"; }
done

# --- model server -----------------------------------------------------------

LMS_URL=$(getenv LMSTUDIO_BASE_URL); LMS_URL=${LMS_URL:-http://localhost:1234/v1}
if curl -sf -m 5 "$LMS_URL/models" >/dev/null 2>&1; then
  say "LM Studio is up"
elif [[ -x "$HOME/.lmstudio/bin/lms" ]]; then
  say "starting LM Studio server"
  "$HOME/.lmstudio/bin/lms" server start >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    curl -sf -m 3 "$LMS_URL/models" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf -m 3 "$LMS_URL/models" >/dev/null 2>&1 \
    && say "LM Studio is up" \
    || warn "LM Studio did not come up; classification and chat will fail"
else
  warn "LM Studio not reachable at $LMS_URL and the lms CLI was not found"
fi

# --- dashboard --------------------------------------------------------------

if [[ ! -d dashboard/node_modules ]]; then
  say "installing dashboard dependencies (first run, this takes a minute)"
  (cd dashboard && npm install --no-audit --no-fund --silent)
fi

if [[ $DEV -eq 0 ]]; then
  # Rebuild when the bundle is missing or older than the source.
  NEWEST=$(find dashboard/src dashboard/index.html -type f -newer dashboard/dist/index.html 2>/dev/null | head -1 || true)
  if [[ $REBUILD -eq 1 || ! -f dashboard/dist/index.html || -n "$NEWEST" ]]; then
    say "building dashboard"
    (cd dashboard && npm run build >/dev/null) || die "dashboard build failed"
  else
    say "dashboard bundle is current"
  fi
fi

# --- voice ------------------------------------------------------------------

if [[ $VOICE -eq 1 ]]; then
  if uv run blackice voice-check >/dev/null 2>&1; then
    say "voice ready — wake word: $(getenv ASSISTANT_NAME)"
    export VOICE_ENABLED=true
  else
    warn "voice prerequisites missing; starting without it:"
    uv run blackice voice-check 2>&1 | sed 's/^/      /' || true
    export VOICE_ENABLED=false
  fi
else
  export VOICE_ENABLED=false
fi

# --- run --------------------------------------------------------------------

DEV_PID=""
cleanup() {
  [[ -n "$DEV_PID" ]] && kill "$DEV_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ $DEV -eq 1 ]]; then
  say "starting Vite dev server on http://localhost:5173"
  (cd dashboard && npm run dev >/dev/null 2>&1) &
  DEV_PID=$!
  say "API on http://localhost:$PORT — open the Vite URL, not this one"
else
  ADDR=$HOST; [[ "$ADDR" == "0.0.0.0" ]] && ADDR=$(ipconfig getifaddr en0 2>/dev/null || echo localhost)
  say "dashboard + API on http://$ADDR:$PORT"
fi

say "Ctrl-C to stop"

if [[ $DEV -eq 1 ]]; then
  # Not exec: that would replace the shell and drop the trap, orphaning Vite.
  uv run blackice serve
else
  exec uv run blackice serve
fi
