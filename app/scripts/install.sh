#!/usr/bin/env bash
# sagent installer.
#
# From a git checkout (the usual way on a server):
#   git clone https://github.com/devjwkim/sagent.git /data/sagent
#   bash /data/sagent/app/scripts/install.sh --host 0.0.0.0
#     → virtualenv /data/sagent/.venv (editable install of the checkout)
#       data       /data/sagent/data
#     update later: cd /data/sagent && git pull && bash app/scripts/install.sh --upgrade
#
# Without a checkout (piped from the web):
#   curl -fsSL https://raw.githubusercontent.com/devjwkim/sagent/main/app/scripts/install.sh | bash
#     → virtualenv ~/.local/share/sagent/venv, data ~/.sagent
#
# Either way the server is started right away (systemd user service when
# available, otherwise a background process) and a one-time link is printed:
# open it to create the first administrator in the browser.
set -euo pipefail

GIT_SOURCE="git+https://github.com/devjwkim/sagent.git#subdirectory=app"
SOURCE=""
PREFIX="${SAGENT_PREFIX:-}"
BIN_DIR="${SAGENT_BIN_DIR:-$HOME/.local/bin}"
DATA="${SAGENT_HOME:-}"
EXTRAS=""
SERVICE=auto
START=1
HOST="127.0.0.1"
PORT="17832"
UPGRADE=0
UNINSTALL=0
PURGE=0
PYTHON="${PYTHON:-}"
WRAPPER_MARK="# sagent-wrapper"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

# --- where are we running from? -------------------------------------------------------
CHECKOUT=""   # repository root when this script lives in a sagent checkout
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ -f "$APP_DIR/pyproject.toml" ] && grep -q '^name = "sagent"' "$APP_DIR/pyproject.toml"; then
    CHECKOUT="$(cd "$APP_DIR/.." && pwd)"
  fi
fi

usage() {
  cat <<USAGE
sagent installer

Options:
  --host HOST       bind address (default: $HOST; 0.0.0.0 for access from other machines)
  --port PORT       port (default: $PORT)
  --source SPEC     what to install: a checkout's app/ directory, a wheel, or a git URL
                    (default: this checkout, or $GIT_SOURCE)
  --prefix DIR      where the virtualenv goes (default: the checkout, or ~/.local/share/sagent)
  --data DIR        data directory, SAGENT_HOME (default: <checkout>/data, or ~/.sagent)
  --bin-dir DIR     where the \`sagent\` command is installed (default: $BIN_DIR)
  --extras LIST     optional extras, e.g. otel,serve
  --service         run as a systemd user service (default when available)
  --no-service      run as a background process instead
  --no-start        install only; do not start the server
  --upgrade         reinstall (after \`git pull\`) and restart the server
  --uninstall       remove the virtualenv, the command and the service (data is kept)
  --purge           with --uninstall: also delete the data directory
  -h, --help        show this help
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --extras) EXTRAS="$2"; shift 2 ;;
    --service) SERVICE=1; shift ;;
    --no-service) SERVICE=0; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    --upgrade) UPGRADE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# --- layout -----------------------------------------------------------------------------
EDITABLE=0
if [ -n "$CHECKOUT" ] && [ -z "$SOURCE" ]; then
  SOURCE="$CHECKOUT/app"
  EDITABLE=1                                  # `git pull` + restart updates the code
  PREFIX="${PREFIX:-$CHECKOUT}"
  DATA="${DATA:-$CHECKOUT/data}"
  VENV="$PREFIX/.venv"
else
  SOURCE="${SOURCE:-$GIT_SOURCE}"
  PREFIX="${PREFIX:-$HOME/.local/share/sagent}"
  DATA="${DATA:-$HOME/.sagent}"
  VENV="$PREFIX/venv"
fi
case "$DATA" in /*) ;; *) DATA="$PWD/$DATA" ;; esac
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/sagent.service"
CMD="$BIN_DIR/sagent"
export SAGENT_HOME="$DATA"

stop_background() {
  pkill -f "^$VENV/bin/python.* $VENV/bin/sagent web" >/dev/null 2>&1 || true
}

if [ "$UNINSTALL" = 1 ]; then
  if [ -f "$UNIT" ] && grep -q "$VENV/bin/sagent" "$UNIT"; then
    systemctl --user disable --now sagent.service >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    say "removed service $UNIT"
  fi
  stop_background
  if [ -L "$CMD" ] || { [ -f "$CMD" ] && grep -q "$WRAPPER_MARK" "$CMD"; }; then
    rm -f "$CMD"; say "removed $CMD"
  fi
  [ -d "$VENV" ] && rm -rf "$VENV" && say "removed $VENV"
  rm -f "$PREFIX/web.log"
  if [ "$PURGE" = 1 ]; then
    [ -d "$DATA" ] && rm -rf "$DATA" && say "deleted data $DATA"
  else
    echo "data kept in $DATA (use --purge to delete it)"
  fi
  [ -z "$CHECKOUT" ] && rmdir "$PREFIX" 2>/dev/null || true
  exit 0
fi

mkdir -p "$DATA"
chmod 700 "$DATA" 2>/dev/null || true

# --- prerequisites ------------------------------------------------------------------------
if [ -z "$PYTHON" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PYTHON="$(command -v "$c")"; break; fi
  done
fi
[ -n "$PYTHON" ] || die "Python 3.11+ is required"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11+ is required ($PYTHON is $("$PYTHON" -V 2>&1))"
"$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1 \
  || die "the Python venv module is missing (Debian/Ubuntu: sudo apt install python3-venv)"
command -v tmux >/dev/null 2>&1 || die "tmux is required (e.g. sudo apt install tmux / brew install tmux)"
command -v git  >/dev/null 2>&1 || die "git is required"
command -v claude >/dev/null 2>&1 || warn "Claude Code (claude) not found — install it to run Claude agents"
command -v codex  >/dev/null 2>&1 || warn "Codex CLI (codex) not found — install it to run Codex agents"

# --- install ----------------------------------------------------------------------------------
if [ -x "$VENV/bin/sagent" ] && [ "$UPGRADE" = 0 ]; then
  say "sagent is already installed in $VENV (use --upgrade to update)"
else
  mkdir -p "$PREFIX"
  [ -d "$VENV" ] || "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  SPEC="$SOURCE"
  if [ -n "$EXTRAS" ]; then
    case "$SOURCE" in
      git+*) SPEC="sagent[$EXTRAS] @ $SOURCE" ;;
      *) SPEC="$SOURCE[$EXTRAS]" ;;
    esac
  fi
  MODE=""; [ "$EDITABLE" = 1 ] && MODE=" (editable)"
  say "installing $SPEC$MODE"
  if [ "$EDITABLE" = 1 ]; then
    "$VENV/bin/python" -m pip install --quiet --upgrade -e "$SPEC"
  else
    "$VENV/bin/python" -m pip install --quiet --upgrade "$SPEC"
  fi
fi

if [ -n "$CHECKOUT" ] && [ -d "$CHECKOUT/.git" ]; then
  # keep `git status` of the checkout clean
  EXCL="$CHECKOUT/.git/info/exclude"
  mkdir -p "$(dirname "$EXCL")"
  for p in "/.venv/" "/data/" "/web.log"; do
    grep -qxF "$p" "$EXCL" 2>/dev/null || echo "$p" >> "$EXCL"
  done
fi

# The command is a tiny wrapper so every `sagent …` uses this data directory.
mkdir -p "$BIN_DIR"
if [ -e "$CMD" ] && [ ! -L "$CMD" ] && ! grep -q "$WRAPPER_MARK" "$CMD"; then
  die "$CMD exists and was not created by this installer; remove it or use --bin-dir"
fi
rm -f "$CMD"
cat > "$CMD" <<WRAPPER
#!/bin/sh
$WRAPPER_MARK (installed by sagent install.sh)
: "\${SAGENT_HOME:=$DATA}"
export SAGENT_HOME
exec "$VENV/bin/sagent" "\$@"
WRAPPER
chmod 755 "$CMD"
say "sagent $("$CMD" --version | awk '{print $2}') → $CMD (data: $DATA)"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH — add it to your shell profile" ;; esac

# --- start the server ---------------------------------------------------------------------
if [ "$SERVICE" = auto ]; then
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then SERVICE=1; else SERVICE=0; fi
fi
if [ "$SERVICE" = 1 ]; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<UNITFILE
[Unit]
Description=sagent web (control plane for CLI coding agents)
After=network.target

[Service]
# PATH captured at install time so the agents (claude, codex, tmux, git) are found
Environment=PATH=$BIN_DIR:$PATH
Environment=SAGENT_HOME=$DATA
WorkingDirectory=$PREFIX
ExecStart=$VENV/bin/sagent web --host $HOST --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNITFILE
  say "wrote $UNIT"
fi

STARTED=0
if [ "$START" = 1 ]; then
  if [ "$SERVICE" = 1 ]; then
    stop_background
    systemctl --user daemon-reload
    systemctl --user enable sagent.service >/dev/null 2>&1
    systemctl --user restart sagent.service
    say "service running: systemctl --user status sagent"
    if command -v loginctl >/dev/null 2>&1 && ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes; then
      warn "to keep it running after logout: sudo loginctl enable-linger $USER"
    fi
  else
    LOG="$PREFIX/web.log"
    stop_background
    # owner-only: the first-run setup link (an admin-creating token) is printed here
    ( umask 077; touch "$LOG" ); chmod 600 "$LOG"
    nohup "$VENV/bin/sagent" web --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
    say "started in the background (log: $LOG)"
  fi
  for _ in $(seq 1 40); do
    if "$VENV/bin/python" -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" >/dev/null 2>&1; then
      STARTED=1; break
    fi
    sleep 0.5
  done
  [ "$STARTED" = 1 ] || warn "the server did not answer on port $PORT yet; check: systemctl --user status sagent"
fi

SHOWN_HOST="$HOST"; [ "$HOST" = "0.0.0.0" ] && SHOWN_HOST="127.0.0.1"
echo
if [ "$STARTED" = 1 ] && SETUP_URLS="$("$CMD" setup-url --host "$HOST" --port "$PORT" 2>/dev/null)"; then
  say "Open this one-time link to create the administrator:"
  echo "$SETUP_URLS" | sed 's/^/  /'
  echo "  (show it again later with: sagent setup-url)"
elif [ "$STARTED" = 1 ]; then
  say "sagent is running: http://$SHOWN_HOST:$PORT"
else
  cat <<NEXT
Next steps:
  sagent doctor                     # check tmux / git / claude / codex
  sagent web --port $PORT            # prints a one-time setup link on first run
NEXT
fi
