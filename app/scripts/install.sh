#!/usr/bin/env bash
# sagent installer — puts sagent in its own virtualenv and links the `sagent`
# command into your PATH. Re-run with --upgrade to update.
#
#   curl -fsSL https://raw.githubusercontent.com/devjwkim/sagent/main/app/scripts/install.sh | bash
#   bash install.sh --host 0.0.0.0       # reachable from other machines on the LAN
#   bash install.sh --no-start           # install only
#   bash install.sh --source ./dist/sagent-0.1.0-py3-none-any.whl
#
# By default the server is started right away (systemd user service when
# available, otherwise a background process) and a one-time link is printed:
# open it to create the first administrator in the browser.
#   bash install.sh --uninstall [--purge]
set -euo pipefail

SOURCE="git+https://github.com/devjwkim/sagent.git#subdirectory=app"
PREFIX="${SAGENT_PREFIX:-$HOME/.local/share/sagent}"
BIN_DIR="${SAGENT_BIN_DIR:-$HOME/.local/bin}"
EXTRAS=""
SERVICE=auto
START=1
HOST="127.0.0.1"
PORT="17832"
UPGRADE=0
UNINSTALL=0
PURGE=0
PYTHON="${PYTHON:-}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  cat <<USAGE
sagent installer

Options:
  --source SPEC     pip install spec: git URL, local path or wheel
                    (default: $SOURCE)
  --prefix DIR      install directory for the virtualenv (default: $PREFIX)
  --bin-dir DIR     where to link the \`sagent\` command (default: $BIN_DIR)
  --extras LIST     optional extras, e.g. otel,serve
  --host HOST       bind address (default: $HOST; 0.0.0.0 for LAN access)
  --port PORT       port (default: $PORT)
  --service         run as a systemd user service (default when available)
  --no-service      run as a background process instead of a service
  --no-start        install only; do not start the server
  --upgrade         upgrade an existing installation
  --uninstall       remove the virtualenv, the command link and the service
  --purge           with --uninstall: also delete data in \${SAGENT_HOME:-~/.sagent}
  -h, --help        show this help
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
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

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/sagent.service"
VENV="$PREFIX/venv"

if [ "$UNINSTALL" = 1 ]; then
  if [ -f "$UNIT" ]; then
    systemctl --user disable --now sagent.service >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    say "removed service $UNIT"
  fi
  if [ -L "$BIN_DIR/sagent" ]; then rm -f "$BIN_DIR/sagent"; say "removed $BIN_DIR/sagent"; fi
  [ -d "$VENV" ] && rm -rf "$VENV" && say "removed $VENV"
  if [ "$PURGE" = 1 ]; then
    DATA="${SAGENT_HOME:-$HOME/.sagent}"
    [ -d "$DATA" ] && rm -rf "$DATA" && say "deleted data $DATA"
  else
    echo "data kept in ${SAGENT_HOME:-$HOME/.sagent} (use --purge to delete it)"
  fi
  exit 0
fi

# --- prerequisites --------------------------------------------------------------
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

# --- install --------------------------------------------------------------------
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
  say "installing $SPEC"
  "$VENV/bin/python" -m pip install --quiet --upgrade "$SPEC"
fi

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/sagent" "$BIN_DIR/sagent"
say "sagent $("$VENV/bin/sagent" --version | awk '{print $2}') → $BIN_DIR/sagent"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH — add it to your shell profile" ;; esac

# --- start the server ---------------------------------------------------------------
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
    systemctl --user daemon-reload
    systemctl --user enable sagent.service >/dev/null 2>&1
    systemctl --user restart sagent.service
    say "service running: systemctl --user status sagent"
    if command -v loginctl >/dev/null 2>&1 && ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes; then
      warn "to keep it running after logout: sudo loginctl enable-linger $USER"
    fi
  else
    LOG="$PREFIX/web.log"
    pkill -f "^$VENV/bin/python.* $VENV/bin/sagent web" >/dev/null 2>&1 || true
    # owner-only: the first-run setup link (an admin-creating token) is printed here
    ( umask 077; touch "$LOG" ); chmod 600 "$LOG"
    nohup "$VENV/bin/sagent" web --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
    say "started in the background (log: $LOG)"
  fi
  for _ in $(seq 1 40); do
    if "$VENV/bin/python" -c "import socket,sys; socket.create_connection(('127.0.0.1', $PORT), 1)" >/dev/null 2>&1; then
      STARTED=1; break
    fi
    sleep 0.5
  done
  [ "$STARTED" = 1 ] || warn "the server did not answer on port $PORT yet; check the log"
fi

SHOWN_HOST="$HOST"; [ "$HOST" = "0.0.0.0" ] && SHOWN_HOST="127.0.0.1"
echo
if [ "$STARTED" = 1 ] && SETUP_URLS="$("$VENV/bin/sagent" setup-url --host "$HOST" --port "$PORT" 2>/dev/null)"; then
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
