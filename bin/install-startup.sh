#!/bin/sh
# bin/install-startup.sh - idempotently wire backup supervisor into RunPod start mechanism.

BACKUP_ROOT="/workspace/_BACKUP"
BACKUP_BIN="${BACKUP_ROOT}/bin"
SUPERVISOR_BIN="${BACKUP_BIN}/supervisor.sh"
ENTRYPOINT_FILE="${BACKUP_BIN}/pod-entrypoint.sh"
SENTINEL_BEGIN="# BEGIN BACKUP SUPERVISOR (install-startup.sh)"
SENTINEL_END="# END BACKUP SUPERVISOR (install-startup.sh)"
HOOK_CANDIDATES="/workspace/start.sh /workspace/startup.sh /workspace/pod-start.sh /workspace/entrypoint.sh /workspace/boot.sh /workspace/init.sh /workspace/pod-entrypoint.sh"

APPLY_REQUESTED=0
DRY_REQUESTED=0
MODE=""
PREVIOUS_START_CMD=""
PID1_CMDLINE=""
WORKSPACE_HOOKS=""
HOOK_SCRIPT=""

log() {
    printf '%s\n' "$*"
}

info() {
    printf 'INFO: %s\n' "$*"
}

warn() {
    printf 'WARN: %s\n' "$*" >&2
}

shell_quote() {
    printf "'"
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
    printf "'"
}

# Parse arguments
while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply)
            APPLY_REQUESTED=1
            ;;
        --dry-run)
            DRY_REQUESTED=1
            ;;
        --start-command)
            shift
            if [ "$#" -eq 0 ]; then
                warn "--start-command requires an argument"
                exit 2
            fi
            PREVIOUS_START_CMD="$1"
            ;;
        --start-command=*)
            PREVIOUS_START_CMD="${1#*=}"
            ;;
        --supervisor)
            shift
            if [ "$#" -eq 0 ]; then
                warn "--supervisor requires an argument"
                exit 2
            fi
            SUPERVISOR_BIN="$1"
            ;;
        --supervisor=*)
            SUPERVISOR_BIN="${1#*=}"
            ;;
        --help)
            cat <<'EOF'
Usage: bin/install-startup.sh [--apply|--dry-run] [--start-command CMD] [--supervisor PATH]

Options:
  --apply             Apply changes (idempotent).
  --dry-run           Only report what would be done.
  --start-command CMD Previous container start command to embed/pass through.
  --supervisor PATH   Path to supervisor executable.
EOF
            exit 0
            ;;
        --)
            shift
            if [ "$#" -gt 0 ]; then
                PREVIOUS_START_CMD="$*"
            fi
            break
            ;;
        -*)
            warn "Unknown option: $1"
            exit 2
            ;;
        *)
            if [ -z "$PREVIOUS_START_CMD" ]; then
                PREVIOUS_START_CMD="$1"
            else
                PREVIOUS_START_CMD="$PREVIOUS_START_CMD $1"
            fi
            ;;
    esac
    shift
done

if [ "$APPLY_REQUESTED" -eq 1 ] && [ "$DRY_REQUESTED" -eq 1 ]; then
    warn "Specify only one of --apply or --dry-run"
    exit 2
fi
if [ "$APPLY_REQUESTED" -eq 1 ]; then
    MODE="apply"
elif [ "$DRY_REQUESTED" -eq 1 ]; then
    MODE="dry-run"
fi

# Detection
log "== Current startup mechanism detection =="

if [ -r /proc/1/cmdline ]; then
    PID1_CMDLINE=$(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null | sed 's/ $//')
fi
log "PID 1 command line: ${PID1_CMDLINE:-unavailable}"

if [ -e /start.sh ]; then
    log "/start.sh: present"
    if [ -r /start.sh ]; then
        first_lines=$(sed -n '1,3p' /start.sh 2>/dev/null)
        printf '%s\n' "$first_lines" | sed 's/^/  /'
    else
        warn "/start.sh is present but not readable"
    fi
else
    log "/start.sh: not present"
fi

log "Existing persistent startup scripts under /workspace:"
if [ -d /workspace ]; then
    WORKSPACE_HOOKS=$(find /workspace -maxdepth 2 -type f \
        \( -name 'start*.sh' -o -name 'boot*.sh' -o -name 'entrypoint*.sh' \
           -o -name 'init*.sh' -o -name '*startup*.sh' -o -name 'pod*.sh' \) \
        -not -path '/workspace/_BACKUP/*' 2>/dev/null | sort -u)
fi
if [ -n "$WORKSPACE_HOOKS" ]; then
    printf '%s\n' "$WORKSPACE_HOOKS" | sed 's/^/  /'
else
    printf '  none found\n'
fi

if [ -n "$WORKSPACE_HOOKS" ]; then
    HOOK_SCRIPT=""
    for candidate in $HOOK_CANDIDATES; do
        if [ -f "$candidate" ]; then
            HOOK_SCRIPT="$candidate"
            break
        fi
    done
    if [ -z "$HOOK_SCRIPT" ]; then
        HOOK_SCRIPT=$(printf '%s\n' "$WORKSPACE_HOOKS" | head -n 1)
    fi
    log "Selected persistent startup hook: $HOOK_SCRIPT"
fi

log "Existing crontab:"
if command -v crontab >/dev/null 2>&1; then
    if crontab -l >/dev/null 2>&1; then
        crontab -l | sed 's/^/  /'
    else
        printf '  none recorded\n'
    fi
else
    printf '  crontab command not available\n'
fi

log "Systemd availability:"
if [ -d /run/systemd/system ]; then
    printf '  systemd appears available (/run/systemd/system exists)\n'
elif command -v systemctl >/dev/null 2>&1; then
    printf '  systemctl exists but systemd may not be running\n'
else
    printf '  no systemd detected\n'
fi

# Determine previous start command if not passed
if [ -z "$PREVIOUS_START_CMD" ]; then
    if [ -n "$PID1_CMDLINE" ]; then
        PREVIOUS_START_CMD="$PID1_CMDLINE"
    elif [ -x /start.sh ]; then
        PREVIOUS_START_CMD="/start.sh"
    fi
fi

if [ -n "$PREVIOUS_START_CMD" ]; then
    log "Detected previous start command: $PREVIOUS_START_CMD"
else
    warn "Could not determine previous container start command."
fi

if [ -z "$MODE" ]; then
    if [ -n "$HOOK_SCRIPT" ]; then
        MODE="apply"
    elif [ -n "$PREVIOUS_START_CMD" ]; then
        MODE="apply"
    else
        MODE="dry-run"
        warn "Ambiguous environment: no persistent hook and no previous start command detected; defaulting to dry-run."
    fi
fi

if [ "$MODE" = "dry-run" ]; then
    log "Running in DRY-RUN mode: no changes will be applied."
else
    log "Running in APPLY mode: changes may be applied."
fi

if [ ! -x "$SUPERVISOR_BIN" ]; then
    warn "Supervisor binary not found or not executable: $SUPERVISOR_BIN"
    warn "It will be referenced behind an -x guard; no start command replacement will be applied until it exists."
fi

if [ -n "$HOOK_SCRIPT" ]; then
    if grep -qF "$SENTINEL_BEGIN" "$HOOK_SCRIPT" 2>/dev/null; then
        log "Supervisor stanza already present in $HOOK_SCRIPT; no change needed."
    else
        log "Existing persistent startup hook found: $HOOK_SCRIPT"
        if [ "$MODE" = "dry-run" ]; then
            log "DRY-RUN: would append guarded supervisor stanza to $HOOK_SCRIPT"
            printf '  %s\n' "$SENTINEL_BEGIN"
            printf '  if [ -x %s ]; then\n' "$(shell_quote "$SUPERVISOR_BIN")"
            printf '      %s >/tmp/backup-supervisor.log 2>&1 &\n' "$(shell_quote "$SUPERVISOR_BIN")"
            printf '  fi\n'
            printf '  %s\n' "$SENTINEL_END"
        else
            if [ ! -w "$HOOK_SCRIPT" ]; then
                warn "Cannot write $HOOK_SCRIPT; skipping append. Re-run with write permission."
            else
                log "Appending guarded supervisor stanza to $HOOK_SCRIPT"
                {
                    printf '\n%s\n' "$SENTINEL_BEGIN"
                    printf 'if [ -x %s ]; then\n' "$(shell_quote "$SUPERVISOR_BIN")"
                    printf '    %s >/tmp/backup-supervisor.log 2>&1 &\n' "$(shell_quote "$SUPERVISOR_BIN")"
                    printf 'fi\n'
                    printf '%s\n' "$SENTINEL_END"
                } >> "$HOOK_SCRIPT" || {
                    warn "Failed to append to $HOOK_SCRIPT"
                    exit 1
                }
                log "Appended to $HOOK_SCRIPT"
            fi
        fi
    fi
else
    if [ -z "$PREVIOUS_START_CMD" ]; then
        warn "Ambiguous: no persistent hook and no previous start command. Cannot generate pod-entrypoint."
        exit 0
    fi

    if [ "$MODE" = "dry-run" ]; then
        log "DRY-RUN: would create $ENTRYPOINT_FILE"
        log "It would launch the supervisor in the background, then exec previous start command: $PREVIOUS_START_CMD"
        log "RunPod Container Start Command to set:"
        printf '  %s\n' "$ENTRYPOINT_FILE"
        exit 0
    fi

    if [ -e "$ENTRYPOINT_FILE" ]; then
        if grep -q 'Generated by bin/install-startup.sh' "$ENTRYPOINT_FILE" 2>/dev/null; then
            log "Existing generated pod-entrypoint.sh found; will overwrite safely."
        else
            warn "Refusing to overwrite existing $ENTRYPOINT_FILE because it was not generated by install-startup.sh."
            warn "Move or remove it manually if it is safe."
            exit 1
        fi
    fi

    mkdir -p "$BACKUP_BIN" 2>/dev/null || {
        warn "Failed to create backup bin directory $BACKUP_BIN"
        exit 1
    }

    escaped_supervisor_bin=$(printf '%s' "$SUPERVISOR_BIN" | sed "s/'/'\\\\''/g")
    escaped_start_cmd=$(printf '%s' "$PREVIOUS_START_CMD" | sed "s/'/'\\\\''/g")

    tmpfile="${ENTRYPOINT_FILE}.tmp.$$"
    umask 022
    cat > "$tmpfile" <<EOF
#!/bin/sh
# Generated by bin/install-startup.sh. Idempotent; re-run may overwrite this file.

SUPERVISOR_BIN='${escaped_supervisor_bin}'
DETECTED_START_CMD='${escaped_start_cmd}'

if [ -x "\$SUPERVISOR_BIN" ]; then
    "\$SUPERVISOR_BIN" >/tmp/backup-supervisor.log 2>&1 &
fi

if [ "\$#" -eq 1 ]; then
    exec /bin/sh -c "\$1"
elif [ "\$#" -gt 1 ]; then
    exec "\$@"
fi

exec /bin/sh -c "\$DETECTED_START_CMD"
EOF
    if [ $? -ne 0 ]; then
        warn "Failed to generate $tmpfile"
        rm -f "$tmpfile"
        exit 1
    fi
    chmod +x "$tmpfile"
    mv "$tmpfile" "$ENTRYPOINT_FILE" || {
        warn "Failed to move $tmpfile to $ENTRYPOINT_FILE"
        rm -f "$tmpfile"
        exit 1
    }
    log "Created/updated $ENTRYPOINT_FILE"

    log "RunPod Container Start Command to set (do this manually in RunPod settings):"
    printf '  %s\n' "$ENTRYPOINT_FILE"
    if [ -n "$PREVIOUS_START_CMD" ]; then
        log "This entrypoint embeds the previous start command: $PREVIOUS_START_CMD"
    fi
fi

log "Done."
