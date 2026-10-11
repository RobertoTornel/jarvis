#!/usr/bin/env bash
#
# Claire - one command that brings the whole system up, and takes it down again.
#
#   ./start.sh                 start everything (idempotent: safe to re-run)
#   ./start.sh check           preflight only: report what is missing, change nothing
#   ./start.sh status          what is running right now
#   ./start.sh stop            stop the panel and the daemon
#   ./start.sh restart         stop, then start
#   ./start.sh --install-service
#                              render deploy/claire.service for this checkout and
#                              install it as a systemd *user* unit
#
# What "everything" is, decided by reading the code rather than guessing:
#
#   * the daemon        `python -m claire`  (src/claire/daemon.py).  It starts the
#                       **phone bridge** itself, right after the listener
#                       (`_start_phone_bridge`, opt out with CLAIRE_PHONE_BRIDGE=0),
#                       so the bridge must NOT be started a second time here: two
#                       recogniser instances on one GPU fault.
#   * the web panel     `python -m claire.panel.server`, a standalone stdlib
#                       server.  Nothing else starts it, so this script does.
#                       Skip it with --no-panel.
#   * the tray app      `python -m desktop_app`, a PyQt6 tray icon and the
#                       low-poly on-screen face.  It needs a desktop session,
#                       so it is the one component that is never started at
#                       boot; `--install-service` wires it to the session.
#                       Skip it with --no-desktop.
#
# Nothing machine-specific is written into this file.  Every path is derived from
# where the script actually lives, and every default is read from the project's
# own code (the panel's default host and port, the repo layout) or from the
# environment.
#
# Environment overrides (all optional; the defaults are portable):
#   CLAIRE_VENV           virtualenv directory        (default: <repo>/.venv)
#   CLAIRE_PYTHON         interpreter used to build the venv, if one is needed
#   CLAIRE_STATE_DIR      pid/log directory           (default: $XDG_STATE_HOME/claire, else ~/.local/state/claire)
#   CLAIRE_LOG_DIR        log directory               (default: <state dir>/logs)
#   CLAIRE_SERVICE_NAME   systemd user unit name      (default: claire)
#   CLAIRE_PANEL_SERVICE_NAME    panel unit name      (default: claire-panel)
#   CLAIRE_DESKTOP_SERVICE_NAME  tray unit name       (default: claire-desktop)
#   CLAIRE_DAEMON_MODE    auto | systemd | direct     (default: auto)
#   CLAIRE_PANEL_MODE     auto | systemd | direct     (default: auto)
#   CLAIRE_DESKTOP_MODE   auto | systemd | direct     (default: auto)
#   CLAIRE_USER_UNIT_DIR  where units are installed   (default: ~/.config/systemd/user)
#   CLAIRE_AUTOSTART_DIR  where the session trigger goes (default: ~/.config/autostart)
#   CLAIRE_PANEL_HOST     panel bind host             (default: the panel's own DEFAULT_BIND_HOST)
#   CLAIRE_PANEL_PORT     panel bind port             (default: the panel's own DEFAULT_PORT)
#   CLAIRE_PREFLIGHT_TIMEOUT  seconds the LLM probe may take (default: 10)
#   CLAIRE_START_WAIT     seconds to wait for a component to come up (default: 60)
#   CLAIRE_FORCE_INSTALL  1 = pip install even while a daemon is live (default: 0)
#
# Exit codes: 0 ok / 2 usage / 3 preflight failed / 4 a component did not come up.

set -euo pipefail

PROG="$(basename -- "${BASH_SOURCE[0]}")"

# --------------------------------------------------------------------------- #
# Locate the repository from the script itself, following symlinks, so that
# `./start.sh`, `/abs/path/start.sh` and a symlink on $PATH behave identically.
# --------------------------------------------------------------------------- #
__self="${BASH_SOURCE[0]}"
while [ -L "$__self" ]; do
    __dir="$(cd -P -- "$(dirname -- "$__self")" && pwd)"
    __self="$(readlink -- "$__self")"
    case "$__self" in
        /*) ;;
        *) __self="$__dir/$__self" ;;
    esac
done
REPO_ROOT="$(cd -P -- "$(dirname -- "$__self")" && pwd)"

# --------------------------------------------------------------------------- #
# Defaults and overrides - all derived, none hardcoded to this machine.
# --------------------------------------------------------------------------- #
SERVICE_NAME="${CLAIRE_SERVICE_NAME:-claire}"
PANEL_SERVICE_NAME="${CLAIRE_PANEL_SERVICE_NAME:-claire-panel}"
DESKTOP_SERVICE_NAME="${CLAIRE_DESKTOP_SERVICE_NAME:-claire-desktop}"
VENV_DIR="${CLAIRE_VENV:-$REPO_ROOT/.venv}"
VENV_PY="$VENV_DIR/bin/python"
REQUIREMENTS="$REPO_ROOT/requirements.txt"
UNIT_TEMPLATE="$REPO_ROOT/deploy/claire.service"
PANEL_UNIT_TEMPLATE="$REPO_ROOT/deploy/claire-panel.service"
DESKTOP_UNIT_TEMPLATE="$REPO_ROOT/deploy/claire-desktop.service"
DESKTOP_AUTOSTART_TEMPLATE="$REPO_ROOT/deploy/claire-desktop.desktop"

: "${XDG_STATE_HOME:=$HOME/.local/state}"
STATE_DIR="${CLAIRE_STATE_DIR:-$XDG_STATE_HOME/claire}"
LOG_DIR="${CLAIRE_LOG_DIR:-$STATE_DIR/logs}"
RUN_DIR="$STATE_DIR/run"
DAEMON_PID_FILE="$RUN_DIR/daemon.pid"
PANEL_PID_FILE="$RUN_DIR/panel.pid"
DESKTOP_PID_FILE="$RUN_DIR/desktop.pid"

USER_UNIT_DIR="${CLAIRE_USER_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
AUTOSTART_DIR="${CLAIRE_AUTOSTART_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/autostart}"
DESKTOP_AUTOSTART_FILE="$AUTOSTART_DIR/claire-desktop.desktop"

START_WAIT="${CLAIRE_START_WAIT:-60}"
PREFLIGHT_TIMEOUT="${CLAIRE_PREFLIGHT_TIMEOUT:-10}"
DAEMON_MODE="${CLAIRE_DAEMON_MODE:-auto}"
PANEL_MODE="${CLAIRE_PANEL_MODE:-auto}"
DESKTOP_MODE="${CLAIRE_DESKTOP_MODE:-auto}"
FORCE_INSTALL="${CLAIRE_FORCE_INSTALL:-0}"

EXIT_OK=0
EXIT_USAGE=2
EXIT_PREFLIGHT=3
EXIT_STARTUP=4

# --------------------------------------------------------------------------- #
# Output.  AGENTS.md asks for an emoji per line and indented detail.
# --------------------------------------------------------------------------- #
say()   { printf '%s\n' "$*"; }
info()  { printf '   %s\n' "$*"; }
ok()    { printf '✅ %s\n' "$*"; }
warn()  { printf '⚠️  %s\n' "$*" >&2; }
bad()   { printf '❌ %s\n' "$*" >&2; }
step()  { printf '\n🔹 %s\n' "$*"; }
die()   { bad "$1"; exit "${2:-1}"; }

# `check` must never write; make that structural, not a promise.
guard_write() { [ "$DRY_RUN" = "1" ] && return 1 || return 0; }

# --------------------------------------------------------------------------- #
# Process helpers.  A pid is only trusted when /proc says it is the process we
# think it is, so a stale pid file after a reboot can never stop the wrong thing.
# --------------------------------------------------------------------------- #
pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

proc_cmdline() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true; }

pid_matches() { # pid marker
    case "$(proc_cmdline "$1")" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

read_file() { cat "$1" 2>/dev/null || true; }

pid_from_file() { # pidfile marker -> echoes the pid only when it is really ours
    local pid
    pid="$(read_file "$1")"
    if pid_alive "$pid" && pid_matches "$pid" "$2"; then
        printf '%s' "$pid"
    fi
}

# TCP liveness without extra tooling: the project's interpreter is a dependency
# this script needs anyway, and the probe is stdlib-only.  It resolves $PYBIN
# itself so a caller that reached it without the preflight still works.
port_open() { # host port
    ensure_pybin
    "$PYBIN" - "$1" "$2" <<'PY' >/dev/null 2>&1
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket()
s.settimeout(0.5)
try:
    sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
finally:
    s.close()
PY
}

wait_for_port() { # host port seconds
    local deadline
    deadline=$(($(date +%s) + $3))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if port_open "$1" "$2"; then return 0; fi
        sleep 0.3
    done
    return 1
}

wait_for_pid_file() { # pidfile seconds -> echoes pid once the file holds one
    local deadline pid
    deadline=$(($(date +%s) + $2))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        pid="$(read_file "$1")"
        if [ -n "$pid" ]; then printf '%s' "$pid"; return 0; fi
        sleep 0.1
    done
    return 1
}

# --------------------------------------------------------------------------- #
# Interpreter and virtualenv.
# --------------------------------------------------------------------------- #
PYBIN=""

version_ok() { # python-binary
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
    if [ -n "${CLAIRE_PYTHON:-}" ]; then
        if command -v "$CLAIRE_PYTHON" >/dev/null 2>&1 && version_ok "$CLAIRE_PYTHON"; then
            command -v "$CLAIRE_PYTHON"
            return 0
        fi
        bad "CLAIRE_PYTHON=$CLAIRE_PYTHON is not a Python 3.11+ interpreter."
        return 1
    fi
    local candidate
    for candidate in python3.12 python3.11 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && version_ok "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# The interpreter this script itself uses when it needs one: the venv's when it
# exists, otherwise any Python 3.11+ on PATH (the panel and this script's tiny
# probes are stdlib-only, so a bare interpreter is enough).
resolve_pybin() {
    if [ -x "$VENV_PY" ]; then
        printf '%s' "$VENV_PY"
        return 0
    fi
    local found
    found="$(find_python 2>/dev/null || true)"
    printf '%s' "${found:-python3}"
}

# Resolve the interpreter into *this* shell.  Everything that probes the machine
# with a Python one-liner (port liveness above all) needs $PYBIN here and now,
# because a command substitution runs in a subshell: a resolution that happens
# inside `$( )` never reaches the caller.  Idempotent, and cheap after the first
# call.
ensure_pybin() {
    [ -n "$PYBIN" ] || PYBIN="$(resolve_pybin)"
}

requirements_hash() {
    "$PYBIN" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$REQUIREMENTS"
}

ensure_venv() {
    if [ -x "$VENV_PY" ]; then
        if version_ok "$VENV_PY"; then
            PYBIN="$VENV_PY"
            return 0
        fi
        bad "$VENV_DIR exists but its Python is $("$VENV_PY" -V 2>&1) - Claire needs 3.11+."
        info "Recreate it deliberately, then run this script again:"
        info "   rm -rf '$VENV_DIR'"
        return 1
    fi

    local base
    if ! base="$(find_python)"; then
        bad "No Python 3.11+ interpreter found on PATH."
        info "Install one (for example: sudo apt install python3.12 python3.12-venv)"
        info "or point this script at one:   CLAIRE_PYTHON=/path/to/python3 ./$PROG"
        return 1
    fi

    say "📦 Creating virtualenv at $VENV_DIR (using $base)"
    if ! guard_write; then
        PYBIN="$base"
        return 0
    fi
    mkdir -p "$(dirname -- "$VENV_DIR")"
    if ! "$base" -m venv "$VENV_DIR"; then
        bad "Could not create the virtualenv."
        info "On Debian/Ubuntu the venv module is a separate package: sudo apt install python3-venv"
        return 1
    fi
    PYBIN="$VENV_PY"
}

daemon_is_live_from_this_venv() {
    local pid
    pid="$(daemon_pid)"
    [ -n "$pid" ] || return 1
    [ -e "/proc/$pid/exe" ] || return 1
    [ "$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)" = "$(readlink -f "$VENV_PY" 2>/dev/null || true)" ]
}

ensure_deps() {
    local stamp="$VENV_DIR/.claire-requirements.sha256" want have
    if [ ! -f "$REQUIREMENTS" ]; then
        bad "requirements.txt is missing from $REPO_ROOT - this is not a Claire checkout."
        return 1
    fi
    want="$(requirements_hash)"
    have="$(read_file "$stamp")"

    if [ "$want" = "$have" ]; then
        ok "Dependencies already installed for this requirements.txt"
        return 0
    fi

    if ! guard_write; then
        say "📦 Would install dependencies:  $VENV_PY -m pip install -r requirements.txt"
        return 0
    fi

    if [ "$FORCE_INSTALL" != "1" ] && daemon_is_live_from_this_venv; then
        warn "Claire is live from $VENV_DIR, so this script will not modify that virtualenv."
        info "Claire running from it is proof the requirements are already installed."
        info "To force a reinstall anyway: stop her first, or pass --force-install / CLAIRE_FORCE_INSTALL=1"
        return 0
    fi

    say "📦 Installing dependencies from requirements.txt"
    if ! "$VENV_PY" -m pip install --disable-pip-version-check -r "$REQUIREMENTS"; then
        bad "pip install failed - Claire cannot start without her dependencies."
        info "Read the pip error above; a missing system library is the common cause"
        info "(for example libportaudio2 for sounddevice)."
        return 1
    fi
    printf '%s\n' "$want" > "$stamp"
    ok "Dependencies installed"
}

# --------------------------------------------------------------------------- #
# systemd user unit (the shipped, always-on path) versus a detached process.
# --------------------------------------------------------------------------- #
systemd_usable() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user list-units >/dev/null 2>&1
}

unit_file_installed() { # NAME
    systemd_usable || return 1
    systemctl --user list-unit-files "$1.service" 2>/dev/null \
        | grep -q "^$1\.service"
}

unit_active() { # NAME
    systemd_usable && systemctl --user is-active --quiet "$1" 2>/dev/null
}

unit_enabled() { # NAME
    systemd_usable && systemctl --user is-enabled --quiet "$1" 2>/dev/null
}

# The three components are supervised the same way, so the helpers are written
# once and named for the component they ask about.
unit_installed() { unit_file_installed "$SERVICE_NAME"; }
systemd_active() { unit_active "$SERVICE_NAME"; }
panel_unit_installed() { unit_file_installed "$PANEL_SERVICE_NAME"; }
panel_systemd_active() { unit_active "$PANEL_SERVICE_NAME"; }
desktop_unit_installed() { unit_file_installed "$DESKTOP_SERVICE_NAME"; }
desktop_systemd_active() { unit_active "$DESKTOP_SERVICE_NAME"; }

daemon_pid() {
    if systemd_active; then
        systemctl --user show "$SERVICE_NAME" -p MainPID --value 2>/dev/null || true
        return 0
    fi
    pid_from_file "$DAEMON_PID_FILE" "claire"
}

daemon_state() {
    if systemd_active; then
        printf 'systemd'
    elif [ -n "$(pid_from_file "$DAEMON_PID_FILE" "claire")" ]; then
        printf 'direct'
    else
        printf 'stopped'
    fi
}

panel_pid() {
    if panel_systemd_active; then
        systemctl --user show "$PANEL_SERVICE_NAME" -p MainPID --value 2>/dev/null || true
        return 0
    fi
    pid_from_file "$PANEL_PID_FILE" "claire.panel.server"
}

panel_state() {
    if panel_systemd_active; then
        printf 'systemd'
    elif [ -n "$(pid_from_file "$PANEL_PID_FILE" "claire.panel.server")" ]; then
        printf 'direct'
    else
        printf 'stopped'
    fi
}

desktop_pid() {
    if desktop_systemd_active; then
        systemctl --user show "$DESKTOP_SERVICE_NAME" -p MainPID --value 2>/dev/null || true
        return 0
    fi
    pid_from_file "$DESKTOP_PID_FILE" "desktop_app"
}

desktop_state() {
    if desktop_systemd_active; then
        printf 'systemd'
    elif [ -n "$(pid_from_file "$DESKTOP_PID_FILE" "desktop_app")" ]; then
        printf 'direct'
    else
        printf 'stopped'
    fi
}

# The panel and the tray app can be supervised by systemd or run detached, in
# the same auto|systemd|direct shape as the daemon: 'auto' prefers the shipped
# unit when it is installed, because that is the path that survives a crash and
# a reboot.
resolve_mode() { # auto|systemd|direct installed?
    local mode="$1" installed="$2"
    if [ "$mode" = "auto" ]; then
        if [ "$installed" = "1" ]; then printf 'systemd'; else printf 'direct'; fi
        return 0
    fi
    printf '%s' "$mode"
}

# Defaults come from the panel's own source, never from a copy in this script.
panel_default() { # ATTRIBUTE
    # This always runs inside a command substitution, so resolve the interpreter
    # in the subshell as well: the parent's $PYBIN is not guaranteed here.
    ensure_pybin
    "$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); import claire.panel.server as s; print(getattr(s, sys.argv[2]))' \
        "$REPO_ROOT/src" "$1" 2>/dev/null || true
}

panel_host() {
    if [ -n "${CLAIRE_PANEL_HOST:-}" ]; then printf '%s' "$CLAIRE_PANEL_HOST"; return; fi
    local h; h="$(panel_default DEFAULT_BIND_HOST)"
    printf '%s' "${h:-127.0.0.1}"
}

panel_port() {
    if [ -n "${CLAIRE_PANEL_PORT:-}" ]; then printf '%s' "$CLAIRE_PANEL_PORT"; return; fi
    panel_default DEFAULT_PORT
}

# --------------------------------------------------------------------------- #
# Component control
# --------------------------------------------------------------------------- #
# Spawn a detached project process.  The wrapper writes its own pid before it
# `exec`s, so the pid file is correct even if `setsid` forks.
_spawn() { # pidfile logfile module-args...
    local pidfile="$1" logfile="$2"; shift 2
    (
        cd "$REPO_ROOT"
        export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
        export PYTHONUNBUFFERED=1
        rm -f "$pidfile"
        if command -v setsid >/dev/null 2>&1; then
            nohup setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$pidfile" "$VENV_PY" "$@" \
                >>"$logfile" 2>&1 &
        else
            nohup bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$pidfile" "$VENV_PY" "$@" \
                >>"$logfile" 2>&1 &
        fi
    )
}

start_daemon() {
    local mode="$DAEMON_MODE"
    if [ "$mode" = "auto" ]; then
        if unit_installed; then mode="systemd"; else mode="direct"; fi
    fi

    local running
    running="$(daemon_pid)"
    if [ -n "$running" ]; then
        ok "Daemon already running ($(daemon_state), pid $running) - left untouched"
        return 0
    fi

    if [ "$mode" = "systemd" ] && ! unit_installed; then
        warn "No systemd user unit named $SERVICE_NAME.service is installed; running the daemon directly instead."
        info "Install the shipped unit with:   ./$PROG --install-service"
        mode="direct"
    fi

    if [ "$mode" = "systemd" ]; then
        if ! guard_write; then
            info "DRY-RUN: systemctl --user start $SERVICE_NAME"
            return 0
        fi
        say "🚀 Starting the daemon: systemctl --user start $SERVICE_NAME"
        if ! systemctl --user start "$SERVICE_NAME"; then
            bad "systemctl could not start $SERVICE_NAME."
            info "See why:   systemctl --user status $SERVICE_NAME"
            info "           journalctl --user -u $SERVICE_NAME -n 50"
            return 1
        fi
        local deadline
        deadline=$(($(date +%s) + START_WAIT))
        while [ "$(date +%s)" -lt "$deadline" ]; do
            if systemd_active; then
                ok "Daemon active under systemd (pid $(daemon_pid))"
                return 0
            fi
            sleep 0.5
        done
        bad "The daemon did not become active within ${START_WAIT}s."
        info "Last log lines:"
        systemctl --user status "$SERVICE_NAME" --no-pager -n 20 2>&1 | sed 's/^/      /' || true
        return 1
    fi

    if ! guard_write; then
        info "DRY-RUN: PYTHONPATH=$REPO_ROOT/src setsid $VENV_PY -m claire >> $LOG_DIR/daemon.log 2>&1"
        return 0
    fi
    mkdir -p "$RUN_DIR" "$LOG_DIR"
    say "🚀 Starting the daemon directly (logs: $LOG_DIR/daemon.log)"
    _spawn "$DAEMON_PID_FILE" "$LOG_DIR/daemon.log" -m claire
    local pid
    pid="$(wait_for_pid_file "$DAEMON_PID_FILE" 5 || true)"
    sleep 2
    if pid_alive "$pid" && pid_matches "$pid" "claire"; then
        ok "Daemon started (pid $pid)"
        return 0
    fi
    bad "The daemon exited immediately."
    info "Last log lines:"
    tail -n 25 "$LOG_DIR/daemon.log" 2>/dev/null | sed 's/^/      /' || true
    return 1
}

start_panel() {
    local host port pid mode installed=0
    ensure_pybin
    host="$(panel_host)"
    port="$(panel_port)"
    if [ -z "$port" ]; then
        bad "Could not read the panel's default port from the project."
        info "Check $REPO_ROOT/src/claire/panel/server.py (it defines DEFAULT_PORT)."
        return 1
    fi

    panel_unit_installed && installed=1
    mode="$(resolve_mode "$PANEL_MODE" "$installed")"

    pid="$(panel_pid)"
    if [ -n "$pid" ]; then
        ok "Panel already running ($(panel_state), pid $pid, http://$host:$port/) - left untouched"
        return 0
    fi
    if port_open "$host" "$port"; then
        warn "Something is already listening on $host:$port; not starting a second panel."
        info "If that is another panel, that is fine. Otherwise set CLAIRE_PANEL_PORT to a free port."
        return 0
    fi

    if [ "$mode" = "systemd" ] && [ "$installed" != "1" ]; then
        warn "No systemd user unit named $PANEL_SERVICE_NAME.service is installed; running the panel directly instead."
        info "Install the shipped units with:   ./$PROG --install-service"
        mode="direct"
    fi

    if [ "$mode" = "systemd" ]; then
        if ! guard_write; then
            info "DRY-RUN: systemctl --user start $PANEL_SERVICE_NAME"
            return 0
        fi
        say "🚀 Starting the web panel: systemctl --user start $PANEL_SERVICE_NAME"
        if ! systemctl --user start "$PANEL_SERVICE_NAME"; then
            bad "systemctl could not start $PANEL_SERVICE_NAME."
            info "See why:   systemctl --user status $PANEL_SERVICE_NAME"
            info "           journalctl --user -u $PANEL_SERVICE_NAME -n 50"
            return 1
        fi
        if wait_for_port "$host" "$port" "$START_WAIT"; then
            ok "Panel is serving http://$host:$port/ (pid $(panel_pid))"
            return 0
        fi
        bad "The panel did not start listening on $host:$port within ${START_WAIT}s."
        info "Last log lines:"
        systemctl --user status "$PANEL_SERVICE_NAME" --no-pager -n 25 2>&1 | sed 's/^/      /' || true
        return 1
    fi

    if ! guard_write; then
        info "DRY-RUN: PYTHONPATH=$REPO_ROOT/src setsid $VENV_PY -m claire.panel.server >> $LOG_DIR/panel.log 2>&1"
        return 0
    fi
    mkdir -p "$RUN_DIR" "$LOG_DIR"

    say "🚀 Starting the web panel on http://$host:$port/ (logs: $LOG_DIR/panel.log)"
    local args=(-m claire.panel.server)
    [ -n "${CLAIRE_PANEL_HOST:-}" ] && args+=(--host "$host")
    [ -n "${CLAIRE_PANEL_PORT:-}" ] && args+=(--port "$port")
    _spawn "$PANEL_PID_FILE" "$LOG_DIR/panel.log" "${args[@]}"
    pid="$(wait_for_pid_file "$PANEL_PID_FILE" 5 || true)"

    if pid_alive "$pid" && wait_for_port "$host" "$port" "$START_WAIT"; then
        ok "Panel is serving (pid $pid)"
        return 0
    fi
    bad "The panel did not start listening on $host:$port within ${START_WAIT}s."
    info "Last log lines:"
    tail -n 25 "$LOG_DIR/panel.log" 2>/dev/null | sed 's/^/      /' || true
    return 1
}

# The tray icon and the on-screen face. A GUI, so it is only ever started into
# an existing session: see deploy/claire-desktop.service for why this one is
# never enabled at boot the way the daemon and the panel are.
start_desktop() {
    local mode installed=0 pid
    desktop_unit_installed && installed=1
    mode="$(resolve_mode "$DESKTOP_MODE" "$installed")"

    pid="$(desktop_pid)"
    if [ -n "$pid" ]; then
        ok "Tray app already running ($(desktop_state), pid $pid) - left untouched"
        return 0
    fi

    if [ "$mode" = "systemd" ] && [ "$installed" != "1" ]; then
        warn "No systemd user unit named $DESKTOP_SERVICE_NAME.service is installed; starting the tray app directly instead."
        info "Install the shipped units with:   ./$PROG --install-service"
        mode="direct"
    fi

    if [ "$mode" = "systemd" ]; then
        if ! guard_write; then
            info "DRY-RUN: systemctl --user start $DESKTOP_SERVICE_NAME"
            return 0
        fi
        say "🚀 Starting the tray app: systemctl --user start $DESKTOP_SERVICE_NAME"
        if ! systemctl --user start "$DESKTOP_SERVICE_NAME"; then
            bad "systemctl could not start $DESKTOP_SERVICE_NAME."
            info "A desktop session is required (DISPLAY and a system tray)."
            info "See why:   systemctl --user status $DESKTOP_SERVICE_NAME"
            info "           journalctl --user -u $DESKTOP_SERVICE_NAME -n 50"
            return 1
        fi
        local deadline
        deadline=$(($(date +%s) + START_WAIT))
        while [ "$(date +%s)" -lt "$deadline" ]; do
            if desktop_systemd_active; then
                ok "Tray app active under systemd (pid $(desktop_pid))"
                return 0
            fi
            sleep 0.5
        done
        bad "The tray app did not become active within ${START_WAIT}s."
        info "Last log lines:"
        systemctl --user status "$DESKTOP_SERVICE_NAME" --no-pager -n 25 2>&1 | sed 's/^/      /' || true
        return 1
    fi

    if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        warn "No DISPLAY or WAYLAND_DISPLAY here, so the tray app cannot open a window."
        info "It starts with your desktop session; skip it with --no-desktop."
        return 1
    fi
    if ! guard_write; then
        info "DRY-RUN: PYTHONPATH=$REPO_ROOT/src setsid $VENV_PY -m desktop_app >> $LOG_DIR/desktop.log 2>&1"
        return 0
    fi
    mkdir -p "$RUN_DIR" "$LOG_DIR"
    say "🚀 Starting the tray app (logs: $LOG_DIR/desktop.log)"
    _spawn "$DESKTOP_PID_FILE" "$LOG_DIR/desktop.log" -m desktop_app
    pid="$(wait_for_pid_file "$DESKTOP_PID_FILE" 5 || true)"
    if pid_alive "$pid"; then
        ok "Tray app started (pid $pid)"
        return 0
    fi
    bad "The tray app did not stay up."
    info "Last log lines:"
    tail -n 25 "$LOG_DIR/desktop.log" 2>/dev/null | sed 's/^/      /' || true
    return 1
}

stop_one() { # label pidfile marker
    local label="$1" pidfile="$2" marker="$3" pid deadline
    pid="$(pid_from_file "$pidfile" "$marker")"
    if [ -z "$pid" ]; then
        rm -f "$pidfile"
        info "$label is not running"
        return 0
    fi
    if ! guard_write; then
        info "DRY-RUN: kill -TERM $pid   # $label"
        return 0
    fi
    say "🛑 Stopping $label (pid $pid)"
    kill -TERM "$pid" 2>/dev/null || true
    deadline=$(($(date +%s) + 20))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        pid_alive "$pid" || break
        sleep 0.3
    done
    if pid_alive "$pid"; then
        warn "$label did not exit after SIGTERM; sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.5
    fi
    rm -f "$pidfile"
    if pid_alive "$pid"; then
        bad "$label (pid $pid) is still alive."
        return 1
    fi
    ok "$label stopped"
}

stop_daemon() {
    if [ "$DO_DAEMON" = "0" ]; then
        info "Daemon left alone (--no-daemon)"
        return 0
    fi
    if systemd_active; then
        if ! guard_write; then
            info "DRY-RUN: systemctl --user stop $SERVICE_NAME"
            return 0
        fi
        say "🛑 Stopping the daemon: systemctl --user stop $SERVICE_NAME"
        systemctl --user stop "$SERVICE_NAME" || true
        if systemd_active; then
            bad "$SERVICE_NAME is still active after stop."
            info "Inspect: systemctl --user status $SERVICE_NAME"
            return 1
        fi
        ok "Daemon stopped (the unit stays enabled, so it returns on next login or start)"
        return 0
    fi
    if [ "$DAEMON_MODE" = "systemd" ]; then
        info "No active $SERVICE_NAME systemd unit"
        return 0
    fi
    stop_one "Daemon" "$DAEMON_PID_FILE" "claire"
}

# Stop a component that can be supervised either by systemd or detached. The
# systemd check comes first and is by *observed state*, not by mode: a unit the
# user started by hand must still be stopped by this script rather than left
# running behind a "nothing to do" message.
stop_component() { # label service-name active-fn pidfile marker direct-only-fn
    local label="$1" unit="$2" is_active="$3" pidfile="$4" marker="$5" mode="$6"
    if "$is_active"; then
        if ! guard_write; then
            info "DRY-RUN: systemctl --user stop $unit"
            return 0
        fi
        say "🛑 Stopping $label: systemctl --user stop $unit"
        systemctl --user stop "$unit" || true
        if "$is_active"; then
            bad "$unit is still active after stop."
            info "Inspect: systemctl --user status $unit"
            return 1
        fi
        ok "$label stopped (the unit stays enabled, so it returns on next login)"
        return 0
    fi
    if [ "$mode" = "systemd" ]; then
        info "No active $unit systemd unit"
        return 0
    fi
    stop_one "$label" "$pidfile" "$marker"
}

stop_panel() {
    if [ "$DO_PANEL" = "0" ]; then
        info "Panel left alone (--no-panel)"
        return 0
    fi
    stop_component "Panel" "$PANEL_SERVICE_NAME" panel_systemd_active \
        "$PANEL_PID_FILE" "claire.panel.server" "$PANEL_MODE"
}

stop_desktop() {
    if [ "$DO_DESKTOP" = "0" ]; then
        info "Tray app left alone (--no-desktop)"
        return 0
    fi
    stop_component "Tray app" "$DESKTOP_SERVICE_NAME" desktop_systemd_active \
        "$DESKTOP_PID_FILE" "desktop_app" "$DESKTOP_MODE"
}

# --------------------------------------------------------------------------- #
# Preflight and self-check
# --------------------------------------------------------------------------- #
PREFLIGHT_FAILED=0

hard_checks() {
    step "Preflight"
    local problems=0

    if [ -f "$REPO_ROOT/src/claire/__main__.py" ]; then
        ok "Repository:  $REPO_ROOT"
    else
        bad "Not a Claire checkout: $REPO_ROOT/src/claire/__main__.py is missing."
        problems=$((problems + 1))
    fi

    if [ -x "$VENV_PY" ]; then
        ok "Virtualenv:  $VENV_DIR ($("$VENV_PY" -V 2>&1))"
    else
        local base
        if base="$(find_python)"; then
            if guard_write; then
                warn "No virtualenv yet at $VENV_DIR - it will be created and dependencies installed."
            else
                ok "Virtualenv:  will be created at $VENV_DIR (using $base)"
            fi
            PYBIN="$base"
        else
            bad "No virtualenv at $VENV_DIR and no Python 3.11+ on PATH."
            info "Install Python 3.11+, or set CLAIRE_PYTHON=/path/to/python3, then re-run."
            problems=$((problems + 1))
        fi
    fi

    if [ ! -f "$REQUIREMENTS" ]; then
        bad "requirements.txt is missing from $REPO_ROOT."
        problems=$((problems + 1))
    fi

    local cfg_dir
    cfg_dir="${XDG_CONFIG_HOME:-$HOME/.config}/claire"
    if [ -d "$cfg_dir" ]; then
        if [ -w "$cfg_dir" ]; then
            ok "Config dir:  $cfg_dir (writable)"
        else
            bad "Config directory is not writable: $cfg_dir"
            info "Fix its permissions, or set XDG_CONFIG_HOME to a directory you own."
            problems=$((problems + 1))
        fi
    elif [ -w "$(dirname -- "$cfg_dir")" ] 2>/dev/null; then
        info "Config dir:  $cfg_dir (not created yet; this script will create it)"
    else
        bad "Cannot create the config directory: $cfg_dir"
        info "Its parent is not writable. Set XDG_CONFIG_HOME to a directory you own."
        problems=$((problems + 1))
    fi

    local env_file="${CLAIRE_ENV_FILE:-$cfg_dir/claire.env}"
    if [ -f "$env_file" ]; then
        ok "Env file:    $env_file"
    else
        warn "No runtime env file at $env_file (so no OMNIROUTE_API_KEY yet)."
        info "Create it, chmod 600, and add the key Claire needs, then re-run:"
        info "   install -m 600 /dev/null '$env_file'"
    fi

    if [ "$problems" -gt 0 ]; then
        PREFLIGHT_FAILED=1
        return 1
    fi
    return 0
}

self_check() {
    step "Claire's own self-check (read-only)"
    if [ ! -x "$VENV_PY" ] || [ ! -f "$REPO_ROOT/src/claire/platform/service.py" ]; then
        info "Skipped (the virtualenv or the project is not in place yet)."
        return 1
    fi
    local out rc
    set +e
    out="$(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$VENV_PY" -m claire.platform.service --self-check --timeout "$PREFLIGHT_TIMEOUT" 2>&1)"
    rc=$?
    set -e
    printf '%s\n' "$out" | sed 's/^/   /'
    return "$rc"
}

command_check() {
    DRY_RUN=1
    hard_checks || true
    local ready=1
    if self_check; then ready=0; fi
    step "Result"
    if [ "$PREFLIGHT_FAILED" = "1" ]; then
        bad "Not startable: fix the items marked above, then run './$PROG check' again."
        return "$EXIT_PREFLIGHT"
    fi
    if [ "$ready" = "0" ]; then
        ok "Ready to start: './$PROG'"
    else
        warn "Claire can start, but the self-check above reports something not ready."
        info "Each line above names its own fix."
        if [ "$STRICT" = "1" ]; then
            return "$EXIT_PREFLIGHT"
        fi
    fi
    return "$EXIT_OK"
}

# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
command_status() {
    local dstate pid state
    step "Claire status"
    ensure_pybin
    dstate="$(daemon_state)"

    case "$dstate" in
        systemd) ok "Daemon:     running under systemd ($SERVICE_NAME.service, pid $(daemon_pid))" ;;
        direct)  ok "Daemon:     running directly (pid $(daemon_pid), pid file $DAEMON_PID_FILE)" ;;
        *)       warn "Daemon:     stopped" ;;
    esac

    state="$(panel_state)"
    pid="$(panel_pid)"
    case "$state" in
        systemd) ok "Panel:      running under systemd ($PANEL_SERVICE_NAME.service, pid $pid, http://$(panel_host):$(panel_port)/)" ;;
        direct)  ok "Panel:      running directly (pid $pid, http://$(panel_host):$(panel_port)/)" ;;
        *)
            if [ -n "$(panel_port)" ] && port_open "$(panel_host)" "$(panel_port)"; then
                ok "Panel:      serving http://$(panel_host):$(panel_port)/ (not started by this script)"
            else
                warn "Panel:      stopped"
            fi
            ;;
    esac

    state="$(desktop_state)"
    pid="$(desktop_pid)"
    case "$state" in
        systemd) ok "Tray app:   running under systemd ($DESKTOP_SERVICE_NAME.service, pid $pid)" ;;
        direct)  ok "Tray app:   running directly (pid $pid)" ;;
        *)       warn "Tray app:   stopped" ;;
    esac

    step "Startup wiring"
    if unit_enabled "$SERVICE_NAME"; then
        ok "Daemon:     starts at login ($SERVICE_NAME.service enabled)"
    elif unit_file_installed "$SERVICE_NAME"; then
        warn "Daemon:     installed but NOT enabled - run ./$PROG --install-service"
    else
        warn "Daemon:     no systemd unit installed - run ./$PROG --install-service"
    fi
    if unit_enabled "$PANEL_SERVICE_NAME"; then
        ok "Panel:      starts at login ($PANEL_SERVICE_NAME.service enabled)"
    elif unit_file_installed "$PANEL_SERVICE_NAME"; then
        warn "Panel:      installed but NOT enabled - run ./$PROG --install-service"
    else
        warn "Panel:      no systemd unit installed - run ./$PROG --install-service"
    fi
    if [ -f "$DESKTOP_AUTOSTART_FILE" ]; then
        if unit_file_installed "$DESKTOP_SERVICE_NAME"; then
            local autostart_note="$(basename -- "$DESKTOP_AUTOSTART_FILE")"
            if unit_enabled "$DESKTOP_SERVICE_NAME"; then
                ok "Tray app:   starts with the desktop session ($autostart_note; $DESKTOP_SERVICE_NAME.service enabled)"
            else
                ok "Tray app:   starts with the desktop session ($autostart_note)"
            fi
        else
            warn "Tray app:   autostart entry present but $DESKTOP_SERVICE_NAME.service is missing"
        fi
    elif unit_enabled "$DESKTOP_SERVICE_NAME"; then
        ok "Tray app:   starts with graphical-session.target ($DESKTOP_SERVICE_NAME.service enabled)"
    else
        warn "Tray app:   not wired into the session - run ./$PROG --install-service"
    fi

    if [ -x "$VENV_PY" ]; then
        info "Virtualenv: $VENV_DIR"
    else
        info "Virtualenv: $VENV_DIR (not created yet)"
    fi
    info "Logs:       $LOG_DIR"
    return "$EXIT_OK"
}

command_start() {
    step "Claire startup"

    ensure_pybin

    hard_checks || true
    if [ "$PREFLIGHT_FAILED" = "1" ]; then
        step "Result"
        bad "Not starting: fix the items marked above, then run './$PROG check' for detail."
        return "$EXIT_PREFLIGHT"
    fi

    if ! ensure_venv; then
        return "$EXIT_PREFLIGHT"
    fi
    if ! ensure_deps; then
        return "$EXIT_PREFLIGHT"
    fi

    local sc_ok=0
    if ! self_check; then sc_ok=1; fi
    if [ "$sc_ok" != "0" ]; then
        warn "The self-check is not all green. Claire is starting anyway; each line above names its fix."
        if [ "$STRICT" = "1" ]; then
            bad "--strict was given, so this script stops here."
            return "$EXIT_PREFLIGHT"
        fi
    fi

    step "Starting components"
    local rc=0
    if [ "$DO_DAEMON" = "1" ]; then
        start_daemon || rc="$EXIT_STARTUP"
    else
        info "Daemon skipped (--no-daemon)"
    fi
    if [ "$DO_PANEL" = "1" ]; then
        start_panel || rc="$EXIT_STARTUP"
    else
        info "Panel skipped (--no-panel)"
    fi
    if [ "$DO_DESKTOP" = "1" ]; then
        start_desktop || rc="$EXIT_STARTUP"
    else
        info "Tray app skipped (--no-desktop)"
    fi

    step "Result"
    if [ "$rc" = "0" ]; then
        ok "Claire is up.  Daemon: $(daemon_state).  Tray app: $(desktop_state)."
        info "Panel:      http://$(panel_host):$(panel_port)/"
        info "Logs:       $LOG_DIR"
        info "Stop her:   ./$PROG stop"
    else
        bad "Something did not come up - see the log lines above."
        info "Re-run the same command to retry; this script is idempotent."
    fi
    return "$rc"
}

command_stop() {
    step "Claire shutdown"
    local rc=0
    stop_desktop || rc=1
    stop_panel || rc=1
    stop_daemon || rc=1
    step "Result"
    if [ "$rc" != "0" ]; then
        bad "Something could not be stopped cleanly - see above."
    elif [ "$DRY_RUN" = "1" ]; then
        info "DRY-RUN: nothing was stopped."
    else
        ok "Stopped. No processes were left behind."
    fi
    return "$rc"
}

command_restart() {
    command_stop || true
    command_start
}

# A shipped template with the checkout path substituted in.  Written in bash so
# an '&', '/' or '\' in the checkout path stays literal (sed replacement text
# does not treat '&' literally).
render_template() { # template
    local template="$1" line
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "${line//@CLAIRE_REPO_ROOT@/$REPO_ROOT}"
    done < "$template"
}

render_unit() { render_template "$UNIT_TEMPLATE"; }

# Render one template onto its destination, staging first so a failure never
# leaves a half-written unit behind.  Sets INSTALL_CHANGED when it wrote.
install_rendered() { # template destination permissions label
    local template="$1" dest="$2" perms="$3" label="$4" staged backup
    if [ ! -f "$template" ]; then
        bad "Template missing: $template"
        return 1
    fi
    if ! grep -q '@CLAIRE_REPO_ROOT@' "$template"; then
        bad "$template does not contain the @CLAIRE_REPO_ROOT@ placeholder."
        info "Refusing to guess; restore it from the repository."
        return 1
    fi
    mkdir -p "$(dirname -- "$dest")"
    staged="$dest.new.$$"
    if ! render_template "$template" > "$staged"; then
        rm -f "$staged"
        bad "Could not render $template."
        return 1
    fi
    if [ -f "$dest" ] && cmp -s "$staged" "$dest"; then
        rm -f "$staged"
        ok "$label already up to date: $dest"
        return 0
    fi
    if [ -f "$dest" ]; then
        backup="$dest.bak.$(date +%Y%m%d%H%M%S)"
        cp -p "$dest" "$backup"
        warn "Existing $label backed up to $backup"
    fi
    chmod "$perms" "$staged"
    mv -f "$staged" "$dest"
    INSTALL_CHANGED=1
    ok "Wrote $dest"
    return 0
}

command_install_service() {
    step "Installing the systemd user unit"
    # No preflight ran on this path, so the interpreter is resolved here: the
    # panel start below probes its port with $PYBIN (see ensure_pybin).
    ensure_pybin
    if ! systemd_usable; then
        bad "No systemd user session here (systemctl --user does not work)."
        info "Run the daemon directly instead: ./$PROG --direct"
        return "$EXIT_PREFLIGHT"
    fi
    local template
    for template in "$UNIT_TEMPLATE" "$PANEL_UNIT_TEMPLATE" \
                    "$DESKTOP_UNIT_TEMPLATE" "$DESKTOP_AUTOSTART_TEMPLATE"; do
        if [ ! -f "$template" ]; then
            bad "Template missing: $template"
            return "$EXIT_PREFLIGHT"
        fi
    done

    if ! guard_write; then
        info "DRY-RUN: render the daemon, panel and tray units into $USER_UNIT_DIR"
        info "DRY-RUN: render the tray autostart entry into $AUTOSTART_DIR"
        info "DRY-RUN: systemctl --user daemon-reload"
        info "DRY-RUN: systemctl --user enable $SERVICE_NAME $PANEL_SERVICE_NAME $DESKTOP_SERVICE_NAME"
        return 0
    fi

    INSTALL_CHANGED=0
    install_rendered "$UNIT_TEMPLATE" "$USER_UNIT_DIR/$SERVICE_NAME.service" \
        644 "daemon unit" || return "$EXIT_PREFLIGHT"
    install_rendered "$PANEL_UNIT_TEMPLATE" "$USER_UNIT_DIR/$PANEL_SERVICE_NAME.service" \
        644 "panel unit" || return "$EXIT_PREFLIGHT"
    install_rendered "$DESKTOP_UNIT_TEMPLATE" "$USER_UNIT_DIR/$DESKTOP_SERVICE_NAME.service" \
        644 "tray unit" || return "$EXIT_PREFLIGHT"
    install_rendered "$DESKTOP_AUTOSTART_TEMPLATE" "$DESKTOP_AUTOSTART_FILE" \
        644 "tray autostart entry" || return "$EXIT_PREFLIGHT"

    systemctl --user daemon-reload || true
    ok "systemd reloaded"
    if [ "$INSTALL_CHANGED" = "1" ] && systemd_active; then
        info "$SERVICE_NAME is running the previous unit; restart it when convenient:"
        info "   systemctl --user restart $SERVICE_NAME"
    fi

    step "Enabling at startup"
    local unit
    for unit in "$SERVICE_NAME" "$PANEL_SERVICE_NAME" "$DESKTOP_SERVICE_NAME"; do
        if systemctl --user enable "$unit" >/dev/null 2>&1; then
            ok "$unit.service enabled"
        else
            warn "Could not enable $unit.service - do it by hand: systemctl --user enable $unit"
        fi
    done
    info "Daemon and panel start at login (and at boot while linger is on)."
    info "The tray app starts with the desktop session via $(basename -- "$DESKTOP_AUTOSTART_FILE")."
    info "On GNOME/KDE it is started by graphical-session.target instead; both are safe together."

    step "Starting now"
    if [ "$DO_DAEMON" = "1" ]; then
        start_daemon || warn "The daemon is not up yet - see above."
    fi
    if [ "$DO_PANEL" = "1" ]; then
        start_panel || warn "The panel is not up yet - see above."
    fi
    if [ "$DO_DESKTOP" = "1" ]; then
        start_desktop || warn "The tray app is not up in this session; it starts with your next desktop login."
    fi
    info "Logs:   journalctl --user -u $SERVICE_NAME -f"
    info "        journalctl --user -u $PANEL_SERVICE_NAME -f"
    info "        journalctl --user -u $DESKTOP_SERVICE_NAME -f"
}

usage() {
    cat <<EOF
Claire - one command that starts (and stops) the whole system.

Usage: ./$PROG [command] [options]

Commands:
  start (default)   bring Claire up (idempotent: safe to re-run)
  stop              stop the panel and the daemon
  restart           stop, then start
  status            what is running right now
  check             preflight and health report only; writes nothing

Options:
  --check              same as the 'check' command
  --dry-run, -n        print every action, execute none
  --no-desktop         do not manage the tray icon / on-screen face
  --no-panel           do not manage the web panel
  --no-daemon          do not manage the daemon
  --systemd            force the daemon through the systemd user unit
  --direct             force the daemon as a detached process
  --strict             refuse to start unless the self-check is all green
  --force-install      pip install even while a Claire daemon is live
  --install-service    install the startup wiring for this checkout: the
                       daemon, panel and tray units plus the tray's autostart
                       entry, all enabled
  -h, --help           this text

Everything machine-specific is resolved at runtime: the repo root from this
script's own location, the venv from the repo, the panel's host and port from
the panel's source, and each component's mode from what is actually installed.
See the header of this file for the environment overrides.

  start            daemon + web panel + tray app (the daemon starts the phone
                   bridge itself)
  stop / restart    clean shutdown, no stray processes
EOF
}

# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
COMMAND=""
DRY_RUN=0
DO_DAEMON=1
DO_PANEL=1
DO_DESKTOP=1
STRICT=0
INSTALL_SERVICE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        start|stop|restart|status|check) COMMAND="$1" ;;
        --check) COMMAND="check" ;;
        --dry-run|-n) DRY_RUN=1 ;;
        --no-panel) DO_PANEL=0 ;;
        --no-daemon) DO_DAEMON=0 ;;
        --no-desktop) DO_DESKTOP=0 ;;
        --systemd) DAEMON_MODE="systemd" ;;
        --direct) DAEMON_MODE="direct" ;;
        --strict) STRICT=1 ;;
        --force-install) FORCE_INSTALL=1 ;;
        --install-service) INSTALL_SERVICE=1 ;;
        -h|--help|help) usage; exit "$EXIT_OK" ;;
        *) bad "Unknown argument: $1"; usage >&2; exit "$EXIT_USAGE" ;;
    esac
    shift
done

[ -n "$COMMAND" ] || COMMAND="start"
[ "$INSTALL_SERVICE" = "1" ] && COMMAND="install-service"

case "$DAEMON_MODE" in
    auto|systemd|direct) ;;
    *) die "CLAIRE_DAEMON_MODE must be auto, systemd or direct (got '$DAEMON_MODE')." "$EXIT_USAGE" ;;
esac

case "$PANEL_MODE" in
    auto|systemd|direct) ;;
    *) die "CLAIRE_PANEL_MODE must be auto, systemd or direct (got '$PANEL_MODE')." "$EXIT_USAGE" ;;
esac

case "$DESKTOP_MODE" in
    auto|systemd|direct) ;;
    *) die "CLAIRE_DESKTOP_MODE must be auto, systemd or direct (got '$DESKTOP_MODE')." "$EXIT_USAGE" ;;
esac

case "$COMMAND" in
    start)            command_start ;;
    stop)             command_stop ;;
    restart)          command_restart ;;
    status)           command_status ;;
    check)            command_check ;;
    install-service)  command_install_service ;;
    *) die "Unknown command: $COMMAND" "$EXIT_USAGE" ;;
esac
