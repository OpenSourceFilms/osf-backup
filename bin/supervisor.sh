#!/usr/bin/env bash
# bin/supervisor.sh - supervise /workspace/_BACKUP/app/backupd.py from a shell.
#
# The supervisor is intentionally independent of Python so it survives a
# Python interpreter-level crash.  It waits for the daemon, logs the exit
# code, delays, and restarts it forever.

set -o pipefail
# Deliberately NOT set -e: this supervisor must survive non-zero child exits.

PYTHON_BIN="/workspace/_BACKUP/.venv/bin/python"
DAEMON_SCRIPT="/workspace/_BACKUP/app/backupd.py"

LOG_DIR="/workspace/_BACKUP/logs"
LOG_FILE="$LOG_DIR/supervisor.log"
LOCK_FILE="$LOG_DIR/supervisor.lock"

IMMEDIATE_EXIT_SECONDS=10   # <10s counts as an immediate/runaway exit
NORMAL_SLEEP_SECONDS=10
MAX_BACKOFF_SECONDS=300

child_pid=""
consecutive_immediate=0
sleep_time="$NORMAL_SLEEP_SECONDS"

logt() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

# Sleep in one-second slices so a trapped signal is not delayed until a long
# foreground sleep completes.
supervisor_sleep() {
    seconds="$1"
    i=0
    while [ "$i" -lt "$seconds" ]; do
        sleep 1
        i=$((i + 1))
    done
}

mkdir -p "$LOG_DIR" || {
    echo "ERROR: cannot create log directory: $LOG_DIR" >&2
    exit 1
}

FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
if [ -z "$FLOCK_BIN" ]; then
    logt "ERROR: flock is required but was not found in PATH"
    echo "ERROR: flock is required but was not found in PATH" >&2
    exit 1
fi

# Open the lock file in append mode.  The fd stays open for the lifetime of
# this supervisor, and the lock is released automatically when the process
# exits.
exec 9>>"$LOCK_FILE" || {
    logt "ERROR: cannot open lock file: $LOCK_FILE"
    echo "ERROR: cannot open lock file: $LOCK_FILE" >&2
    exit 1
}

if ! "$FLOCK_BIN" -n 9; then
    logt "ERROR: another supervisor instance holds lock: $LOCK_FILE"
    echo "ERROR: another supervisor instance holds lock: $LOCK_FILE" >&2
    exit 1
fi

logt "supervisor pid $$ acquired lock on $LOCK_FILE"

cleanup() {
    trap '' TERM INT   # avoid re-entering cleanup on duplicate signals
    sig="$1"

    logt "supervisor received SIG$sig"

    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        logt "forwarding SIG$sig to child pid $child_pid"
        kill "-$sig" "$child_pid" 2>/dev/null || true

        # Give the daemon up to 10 seconds to shut down cleanly.
        i=0
        while [ "$i" -lt 10 ]; do
            kill -0 "$child_pid" 2>/dev/null || break
            i=$((i + 1))
            sleep 1
        done

        if kill -0 "$child_pid" 2>/dev/null; then
            logt "child pid $child_pid did not stop after SIG$sig; sending SIGKILL"
            kill -KILL "$child_pid" 2>/dev/null || true
        fi

        wait "$child_pid" 2>/dev/null || true
    else
        logt "no child process is running"
    fi

    logt "supervisor exiting cleanly after SIG$sig"
    exit 0
}

trap 'cleanup TERM' TERM
trap 'cleanup INT' INT

if [ ! -x "$PYTHON_BIN" ]; then
    logt "ERROR: python executable not found or not executable: $PYTHON_BIN"
    echo "ERROR: python executable not found or not executable: $PYTHON_BIN" >&2
    exit 1
fi

if [ ! -r "$DAEMON_SCRIPT" ]; then
    logt "ERROR: daemon script not found or not readable: $DAEMON_SCRIPT"
    echo "ERROR: daemon script not found or not readable: $DAEMON_SCRIPT" >&2
    exit 1
fi

RESOLVED_PYTHON="$PYTHON_BIN"

while :; do
    # Close fd 9 for the child so the daemon does not inherit the supervisor
    # lock.  The supervisor keeps the lock on fd 9.
    "$RESOLVED_PYTHON" "$DAEMON_SCRIPT" 9>&- &
    child_pid=$!
    start_epoch="$(date +%s)"

    banner_msg="supervisor starting daemon: python=$RESOLVED_PYTHON daemon_pid=$child_pid"
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$banner_msg"
    logt "$banner_msg"

    wait "$child_pid"
    status=$?
    exited_pid="$child_pid"
    child_pid=""

    end_epoch="$(date +%s)"
    runtime_seconds=$((end_epoch - start_epoch))

    logt "daemon pid $exited_pid exited with code $status after ${runtime_seconds}s"

    if [ "$runtime_seconds" -lt "$IMMEDIATE_EXIT_SECONDS" ]; then
        consecutive_immediate=$((consecutive_immediate + 1))
        if [ "$consecutive_immediate" -eq 1 ]; then
            sleep_time="$NORMAL_SLEEP_SECONDS"
        else
            sleep_time=$((sleep_time * 2))
        fi

        if [ "$sleep_time" -gt "$MAX_BACKOFF_SECONDS" ]; then
            sleep_time="$MAX_BACKOFF_SECONDS"
        fi

        logt "immediate exit detected (${runtime_seconds}s < ${IMMEDIATE_EXIT_SECONDS}s; consecutive=$consecutive_immediate); backing off ${sleep_time}s"
    else
        consecutive_immediate=0
        sleep_time="$NORMAL_SLEEP_SECONDS"
        logt "daemon ran for ${runtime_seconds}s; sleeping ${sleep_time}s before restart"
    fi

    supervisor_sleep "$sleep_time"
done
