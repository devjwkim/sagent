#!/usr/bin/env bash
# sagent installer — puts sagent in its own virtualenv and links the `sagent`
# command into your PATH. Re-run with --upgrade to update.
#
#   curl -fsSL https://raw.githubusercontent.com/devjwkim/sagent/main/app/scripts/install.sh | bash
#   bash install.sh --service            # also run `sagent web` as a systemd user service
#   bash install.sh --source ./dist/sagent-0.1.0-py3-none-any.whl
#   bash install.sh --uninstall [--purge]
set -euo pipefail

SOURCE="git+https://github.com/devjwkim/sagent.git#subdirectory=app"
PREFIX="${SAGENT_PREFIX:-$HOME/.local/share/sagent}"
BIN_DIR="${SAGENT_BIN_DIR:-$HOME/.local/bin}"
EXTRAS=""
SERVICE=0
START=1
HOST="127.0.0.1"
PORT="7832"
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
  --service         install a systemd user service running \`sagent web\`
  --host HOST       service bind address (default: $HOST)
  --port PORT       service port (default: $PORT)
  --no-start        write the service file but do not enable/start it
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

# --- optional service -------------------------------------------------------------
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
  if [ "$START" = 1 ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now sagent.service
    say "service started: systemctl --user status sagent"
    command -v loginctl >/dev/null 2>&1 && loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes \
      || warn "to keep it running after logout: sudo loginctl enable-linger $USER"
  fi
fi

WEB_NOTE="sagent web                        # http://$HOST:$PORT"
if [ "$SERVICE" = 1 ] && [ "$START" = 1 ]; then WEB_NOTE="open http://$HOST:$PORT              # the service is already running"; fi
cat <<NEXT

Next steps:
  sagent doctor                     # check tmux / git / claude / codex
  sagent user create-admin          # first administrator
  $WEB_NOTE
NEXT
