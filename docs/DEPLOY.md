# Deploying to the CPU-only pod

This system was **built from the GPU pod** (`dokilqcrpplxs3`, EU-RO-1). It is
intended to **run on the always-on CPU-only pod**. Both mount the same
`/workspace` network volume (`cmkgi43hcx`), so the code, venv, config and state
are already in place on the CPU pod at `/workspace/_BACKUP` — there is nothing
to copy.

What could **not** be verified from the build session is the CPU pod's own
startup mechanism. Everything in this document is therefore a hand-off
procedure to run *there*, in order, with a verification step after each.

> **Do not skip step 2.** On RunPod, a custom *Container Start Command*
> overrides the image's `CMD`. Replacing an unknown start command can leave the
> pod with no SSH, no Jupyter and no way back in. `install-startup.sh` refuses
> to overwrite an unknown command by design.

---

## 0. Prerequisites

The one thing that cannot be done from any Claude session is credentials.

| Item | Where it comes from |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | RunPod console → Settings → **S3 API Keys**. This is *not* the RunPod API key. |
| `BACKUP_NTFY_URL` / `BACKUP_NTFY_ENABLED` | **Primary daily push notification.** Generate a random topic (`osf-backup-<32 hex chars>`, e.g. via `python3 -c "import secrets;print('osf-backup-'+secrets.token_hex(16))"`) -- the topic name IS the access secret, never a descriptive name. Set `BACKUP_NTFY_URL=https://ntfy.sh/<topic>` and `BACKUP_NTFY_ENABLED=1`. Not optional for production readiness (see README.md "Notifications"). |
| `BACKUP_HEALTHCHECKS_URL` | Independent dead-man switch: healthchecks.io → new check → its ping URL (see step 6 for the schedule/grace to configure there). |
| `BACKUP_EMAIL_ENABLED` / `BACKUP_EMAIL_TO` / `BACKUP_SMTP_HOST` / `BACKUP_SMTP_USERNAME` / `BACKUP_SMTP_PASSWORD` | **Optional, off by default** -- no SMTP credential is required for production readiness now that ntfy is primary (see README.md "Notifications"). `BACKUP_SMTP_PASSWORD` is a Gmail **App Password**, never the account password. Leave `BACKUP_EMAIL_ENABLED` unset to skip entirely. |

Set them on the CPU pod as pod environment variables (RunPod console → Edit
Pod → Environment Variables), not in any file under `/workspace`. The system
reads them from the environment only, and refuses to read credentials from
config.

Verify they arrived, without printing them:

```bash
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY BACKUP_NTFY_URL BACKUP_HEALTHCHECKS_URL; do
  [ -n "${!v}" ] && echo "$v: SET (${#v} chars)" || echo "$v: MISSING"
done
```

## 1. Verify the install came across

```bash
cd /workspace/_BACKUP
readlink -f .venv/bin/python     # must resolve under /workspace, not /usr/bin
./.venv/bin/python -c "import boto3, zstandard, yaml; print('deps OK')"
./bin/backupctl doctor
```

`doctor` exercises each dependency rather than checking it exists. Expect the
S3 checks to FAIL until step 0 is done. Everything else should PASS.

If `.venv/bin/python` resolves under `/usr/bin`, the interpreter is ephemeral —
rebuild the venv from the lockfile before continuing:

```bash
uv venv --python /workspace/.pod-persist/uv-python/cpython-3.13.8-linux-x86_64-gnu/bin/python3 .venv
uv pip install --python .venv/bin/python -r requirements.lock.txt
```

## 2. Discover the CPU pod's existing startup mechanism

**Read-only. Change nothing yet.**

```bash
ps -p 1 -o pid,comm,args           # what PID 1 actually is
ls -la /start.sh 2>/dev/null       # does the RunPod image entrypoint exist?
crontab -l 2>/dev/null             # any existing cron
ls -la /workspace/.pod-persist/bin/quick-init.sh
```

Then capture the pod's **current Container Start Command** from the RunPod
console (Edit Pod → Container Start Command). If that field is **empty**, the
image `CMD` is in force and there is nothing to preserve. If it is **not**
empty, copy it verbatim — step 3 needs it, and it must not be lost.

```bash
./bin/install-startup.sh --dry-run
```

This prints what it *would* do and applies nothing. Read its output before
proceeding. It defaults to dry-run whenever the environment is ambiguous.

## 3. Wire in the supervisor

Preference order, handled automatically by the script:

1. **An existing persistent startup script under `/workspace`** — the stanza is
   appended between sentinel comments, so re-running is idempotent.
2. **No such script** — it writes `bin/pod-entrypoint.sh`, which starts the
   supervisor in the background and then `exec`s the previous start command,
   and prints the exact Container Start Command for you to set **manually**.

```bash
./bin/install-startup.sh              # apply
```

If it takes route 2, set the printed value as the Container Start Command in
the RunPod console yourself. The script never applies it for you.

## 4. Bring it up and prove it

```bash
nohup ./bin/supervisor.sh >/dev/null 2>&1 &
sleep 5
./bin/backupctl status
```

Then the ordering that matters, per the build plan:

```bash
./bin/backupctl dry-run          # STOP. Review the report before seeding.
```

**Do not run a real backup until the dry-run classification is approved.**
Once it is:

```bash
./bin/backupctl run              # seed
./bin/backupctl verify
./bin/backupctl restore --snapshot $(date +%F) --path <small-known-path> \
                        --destination /workspace/_restore_test
```

A backup that has never been restored is not validated. Do the restore drill.

## 5. Prove it survives a restart

```bash
pkill -f backupd.py              # supervisor must restart it within ~10s
sleep 15 && ./bin/backupctl status
```

Then one controlled **pod** restart from the console, and afterwards confirm:

- the daemon came back automatically;
- it did **not** re-run a backup that already succeeded today;
- `backupctl status` shows the next run as the next Melbourne 04:00 (from
  `config/runtime.yaml`'s `schedule.time` -- see README.md "Scheduling").

## 6. Healthchecks configuration (independent dead-man switch)

On healthchecks.io, set the check to:

| Field | Value |
|---|---|
| Schedule | `0 4 * * *` |
| Timezone | `Australia/Melbourne` |
| Grace | **26 hours initially** — observed real run durations range roughly 9-24h; a tighter grace like 8h will false-alarm on a normal long run |

The grace period is what catches a *hung* run: a `/start` ping with no success
inside the grace window is reported as a failure, not just a missing run. If
the pod is dead entirely, nothing on it can report that — the absent ping is
the signal. Paste the ping URL into `BACKUP_HEALTHCHECKS_URL` (in
`config/credentials.env`, matching step 0). This channel is deliberately
independent of ntfy below — see README.md "Notifications" for why both exist.

## 7. ntfy push notifications (primary daily channel)

`BACKUP_NTFY_URL`/`BACKUP_NTFY_ENABLED` should already be set from step 0 (a
random, unguessable topic — the topic name IS the access secret on the public
ntfy.sh service). Confirm the operator has subscribed to the topic in the
ntfy phone app, then run:

```bash
./bin/backupctl test-ntfy
```

`status: sent` plus an HTTP 2xx means the call succeeded — **you must still
separately confirm the push actually arrived on the phone**, since a 2xx only
proves ntfy.sh accepted the publish, not that anyone is subscribed. See
README.md "Notifications" for the full behavior — a broken/misconfigured ntfy
setup never blocks or fails a backup, only the push about it.

## 8. Email notifications (optional, off by default)

If `BACKUP_EMAIL_*`/`BACKUP_SMTP_*` were set in step 0, run:

```bash
./bin/backupctl test-email
```

Email is optional and off by default now that ntfy is the primary channel --
no SMTP credential is required for production readiness. Run this only if
email is specifically wanted in addition to ntfy. `status: sent` means
delivery is confirmed; `disabled` means `BACKUP_EMAIL_ENABLED` is unset
(intentionally skipping the feature, not an error); anything else needs
fixing before enabling it. See README.md "Notifications" for the full
behavior -- a broken/misconfigured email setup never blocks or fails a
backup, only the notification about it.

## 9. Local staging sanity check

```bash
./bin/backupctl staging-status
```

Expect a small figure (idle: ~0.5-1 GiB, dominated by `state.sqlite`; a
few GiB at most during an active transaction) with no `state/blob_cache/`
warning and no staging flagged as belonging to a `SUCCESS` transaction. See
README.md "Local staging" for what each category means and why this must
never scale with the protected backup set's size.

---

## Rollback

The system touches nothing outside `/workspace/_BACKUP` except the startup
stanza added in step 3.

```bash
pkill -f supervisor.sh; pkill -f backupd.py
# remove the sentinel-delimited block from the startup script, or
# restore the previous Container Start Command in the console
```

Removing `/workspace/_BACKUP` removes the local cache. It does **not** remove
anything already uploaded — the remote repository is self-describing and
`backupctl rebuild-state` can reconstruct local state from it.
