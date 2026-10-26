# OSF Selective Backup

> **Historical implementation, not a current deployment guide (October 2026).**
> This repository documents OSF's earlier RunPod S3-compatible, selective
> content-addressed backup design. It is retained for its reusable engineering
> ideas, including conservative file classification, crash-resumable manifests,
> recovery independent of local caches, and explicit storage-capacity admission.
> The pod identities, volume paths, credentials workflow, capacities and startup
> instructions below describe that historical environment. They are **not
> verified for any present OSF installation** and should not be applied to a
> live pod without independent configuration, rights review, recovery testing
> and confirmation of the deployment target. No actual backup data or
> credentials are provided here.

## Source availability and reuse status

**Publicly viewable does not currently mean open-source licensed.** This
historical repository does not yet contain a repository-wide `LICENSE` or
`COPYING` file. Authorship, third-party materials and the intended
redistribution licence are being reviewed in
[issue #1](https://github.com/OpenSourceFilms/osf-backup/issues/1).
Until an authorised licence is provided, **do not assume that MIT terms from
other OSF repositories apply here**, or that publication grants general
permission to redistribute or modify the code beyond GitHub's terms and
applicable law.

This is a description of the current permission boundary, not a new licence
or a claim of sole OSF ownership. It does not change the existing historical
status, and nothing in this repository is a current B2/restic deployment
instruction.

## Third-party dependency licences

The 14 Python dependencies pinned in `requirements.lock.txt` have a
[dated upstream source-licence inventory](DEPENDENCY_LICENSES.md), with links
to the publishers' actual licence files and distinctions between packages,
built wheels and external runtime services. These licences **do not
automatically licence the OSF backup implementation**. The owner/contributor
rights decision remains open in [issue #1](https://github.com/OpenSourceFilms/osf-backup/issues/1).

## Public offline CI

This historical snapshot has [automatic offline regression tests](.github/workflows/offline-tests.yml).
They run **245 synthetic/local tests** on a clean Python 3.11 runner, and
explicitly exclude the five tests marked `live` that exercise real S3
storage. Dependency packages are downloaded during environment setup;
the actual pytest phase runs with no cloud credentials and a Python socket
guard that rejects IPv4/IPv6 connections.

The previous regression for classifying a clean remote-backed Git checkout
now builds its own temporary bare Git remote instead of requiring an OSF
private directory under `/workspace`. This tests the same functionality on
any clean checkout without touching a production Git repository.

**The network guard is defence in depth for these tests, not a hardened
sandbox or proof that arbitrary third-party code cannot reach the network.**
A passing CI result verifies offline unit/integration behaviour, not live
backup, recovery, credentials, continuity, or suitability for deployment.
The current OSF B2/restic backup system is separate and is unaffected.

A selective, deduplicating, resumable backup service for the `/workspace`
network volume, writing to the RunPod network volume `v6a4zjiytb` over its
S3-compatible API.

**Install root:** `/workspace/_BACKUP` (on the shared network volume, so it
survives pod rebuilds and is reachable from every pod that mounts it).

**Original intended runtime:** an always-on CPU-only pod. It was *built* from the GPU pod —
both mount the same `/workspace`, but the CPU pod's startup mechanism was not
inspectable from the build session. See `docs/DEPLOY.md` before enabling
autostart there.

---

## What it is not

It is not a mirror. It deliberately does **not** copy `/workspace` wholesale:
the source volume is measured in terabytes and the backup target holds 100 GB.
The system's entire job is deciding *what deserves to be there*, and being loud
about everything it left out.

Three failure modes drive every design decision:

1. **Silently not protecting something important.** Hence: unclassified data is
   never silently dropped — it is reported. Ambiguous git repos are backed up,
   not assumed safe. Critical data is never discarded to stay under quota; the
   run fails loudly instead.
2. **Filling 100 GB with junk.** Hence: quotas on generated outputs,
   content-addressed deduplication, and an explicit capacity ledger.
3. **Faithfully propagating an accidental deletion.** Hence: a deleted source
   file does not delete its backup. Snapshots retain the reference until
   retention expires, then a 30-day orphan grace runs before GC.

## Core model

- **Content-addressed blobs.** Every file is stored once under
  `blobs/sha256/<ab>/<digest>`. Renaming a file costs no new storage. The
  system's own SHA-256 is authoritative — an S3 ETag is never treated as
  content identity.
- **Snapshots are manifests, not copies.** A daily manifest references blobs.
  Keeping 28 snapshots of unchanged data costs 28 small manifests.
- **One logical transaction per Melbourne calendar day**, resumable. A crash
  mid-upload does not lose the day; the daemon resumes the same run. A snapshot
  does not exist until its manifest is committed.
- **Restore does not depend on local state.** `state.sqlite` is a cache. The
  remote manifests are the authoritative recovery record.

### Cached SHA-256 integrity

The hash cache is an optimization, not authority over source contents.
Path, byte count and modification time are **not sufficient** to reuse an
earlier digest: applications can replace files with new bytes of identical
size while preserving `mtime`. The runner's `known_hashes_v2` records
therefore also bind each digest to the actual upload source's device, inode
and filesystem change time (`ctime_ns`). The first run after upgrading
rehashes files that have only legacy `known_hashes` entries; those historical
records are left intact but never trusted as cache hits. Staged database or
git-rescue artifacts are checked against the **staged file**, not merely
their logical source path. Changes detected while hashing or between source
checks make that entry fail/defer rather than reusing a stale hash.

These metadata checks prevent the common same-size/same-mtime replacement
failure mode, but do **not** constitute an immutable filesystem snapshot,
a guarantee against hostile concurrent mutation or a substitute for remote
blob verification. The public patch does not alter any running backup daemon
or state database until an independently approved deployment.

## Layout

```
/workspace/_BACKUP/
├── app/          # implementation
├── bin/          # backupctl, supervisor.sh, install-startup.sh
├── config/       # policy.yaml (what to back up), runtime.yaml (when/how the daemon runs)
├── state/        # state.sqlite      (never backed up)
├── reports/      # inventory + dry-run reports
├── logs/         # operational logs  (never backed up)
├── tmp/          # staging           (never backed up)
├── tests/
└── .venv/        # never backed up
```

`app/`, `bin/`, `config/`, `tests/` and this README **are** backed up — the
backup system is itself important data.

## Remote layout

```
s3://v6a4zjiytb/_osf_backup/
├── blobs/sha256/<ab>/<digest>
├── snapshots/daily/<YYYY-MM-DD>.json.zst
├── catalogs/<timestamp>.sqlite.zst
└── meta/{latest.json,snapshot-index.json,repository.json}
```

## Usage

```bash
cd /workspace/_BACKUP
./bin/backupctl status        # phase, last success, next run
./bin/backupctl doctor        # exercise every dependency
./bin/backupctl inventory     # classify the source, no upload
./bin/backupctl dry-run       # full report, no upload
./bin/backupctl run           # one logical run for today
./bin/backupctl retry         # retry a FAILED date
./bin/backupctl snapshots     # list committed snapshots
./bin/backupctl verify        # sampled download verification
./bin/backupctl restore --snapshot 2026-08-15 --path projects/foo \
                        --destination /workspace/_restore_test
./bin/backupctl rebuild-state # reconstruct state.sqlite from remote
```

### Historical restore: symlink containment

The `app.restore.restore()` reader now rechecks path containment as
manifest entries are applied, not only when it first parses the manifest.
An earlier manifest entry may create a symlink, and a subsequent file
must not follow that newly created link outside the requested destination.
The code rechecks containment before creating parent directories or
reading the corresponding backup object. It also rechecks directories
before applying final permissions and modification times.

This preserves symlinks pointing **within** the chosen restore tree, while
refusing out-of-tree writes. Three synthetic tests cover absolute and nested
escaping symlinks, zero external blob reads on rejection, and valid
in-tree symlink recovery. These checks do not create a cross-process
filesystem lock against a concurrently hostile actor changing paths while
the restore is running. For real recovery, use a newly created,
access-restricted restore destination and independently inspect the
snapshot before approving any write to production data.

Restored symlinks are also staged under a unique same-directory temporary
name and atomically published with `os.replace()` **only after** the
replacement link and its metadata are ready. Previously, replacing a regular
file or symlink first unlinked the existing destination, then attempted to
create the restored link. A failed link creation could therefore delete
previously accepted data. Five synthetic regression tests verify that failed
link creation, timestamp updates or publication leave an existing destination
intact, that successful replacement works, and that identical symlinks remain
skipped. The staging approach does **not** make a multi-entry restore
transactional or defend against an attacker mutating the destination tree.

**Duplicate manifest destinations:** A restore now checks the selected
entries for repeated *normalized* destination paths before creating
directories or downloading backup objects. For example,
`/workspace/shot/plate.png` and `workspace/shot/./plate.png` refer to
the same destination. Without validation, the last entry could silently
overwrite a different file from an earlier entry while both were counted
as successfully restored. The restore now reports an ambiguity and applies
**none of the selected entries** in this case. Four offline synthetic tests
cover differing and identical duplicate files, directory/file conflicts,
preservation of previously approved data, and prefix-filtered restores.
This does not make a normal multi-entry restore transactional, and does
not resolve distinct lexical paths that lead to the same target through
a symlink.

**Directory metadata order:** Directory creation and file restoration finish
before archived directory modes and modification times are applied. Those
final metadata operations now run **deepest first**, independent of manifest
ordering. Otherwise a read-only ancestor restored first could prevent a
non-root process from finalising nested directories. Three offline cases
cover both manifest orderings and a read-only tree containing a file.
This does not make a restore atomic or change permission policy.

This is a correction to the **historical public implementation**, not a
change or acceptance result for the current B2/restic installation.

## Classification

| Class | Treatment |
|---|---|
| `CRITICAL` | Always backed up. No per-file size cap. |
| `BTS` | Backed up. Exempt from generated-output caps. |
| `PRINCIPAL` | Excluded — by explicit path only, never by extension. |
| `REDOWNLOADABLE` | Payload excluded; reconstruction manifest recorded. |
| `OUTPUTS` | Backed up within per-file / per-root / global quotas. |
| `UNCLASSIFIED` | **Not** backed up, **always** reported. |

Directory markers override configured rules:

- `.backup-always` — recursive; critical, and exempt from output quotas.
- `.backup-never` — recursive; excluded.

Neither marker can override the hard exclusions protecting credentials and the
backup system's own state.

## Git handling

A `.git` directory does **not** mean "safe on a remote". A repo is treated as
`GIT_REMOTE_BACKED` (tracked files skipped, tiny metadata recorded) only when
*all* of: a remote exists, nothing staged, nothing unstaged, no non-ignored
untracked files, the branch has an upstream, it is not ahead of that upstream,
and there are no local-only branch tips.

Anything else — including detached HEAD, no upstream, or any inspection error —
becomes `GIT_RESCUE`: a rescue package (patches, untracked archive, commit
bundle) is stored. **Repositories are never mutated** to find this out; no
`fetch`, no `gc`. If a complete rescue package cannot be built, the repo is
backed up in full rather than skipped.

`UNCERTAIN ⇒ BACK UP`, never `UNCERTAIN ⇒ ASSUME GITHUB HAS IT`.

## Scheduling

**04:00 `Australia/Melbourne`** — the default comes from `config/runtime.yaml`'s
`schedule.time` (shared by `backupd`, `backupctl status`, and `backupctl doctor`
via `app.wiring._load_runtime_yaml`, so they can never disagree), overridable
per-invocation via `run_hour`/`run_minute` in backupd's JSON `--config` file.
Deliberately kept out of `config/policy.yaml`: `Policy.policy_hash` must never
change just because the run time changed — see `config/runtime.yaml`'s header
comment. Via `zoneinfo` — DST transitions are
handled by comparing real POSIX instants (`.timestamp()` on both sides of
the diff), never by adding 86400 seconds or subtracting two datetimes that
share one `ZoneInfo` instance (that silently drops the DST offset).

On daemon start/wake, decisions are keyed off both today's transaction phase
and whether today's 04:00 slot has already passed:

| Today's transaction | Now vs. 04:00 today | Action |
|---|---|---|
| `SUCCESS` | — | sleep until tomorrow's 04:00 |
| in progress (crash/orphan resume) | — | resume the same run immediately, regardless of clock time |
| never started | before 04:00 | wait until 04:00, then re-decide |
| never started | at/after 04:00 | catch-up run immediately |
| `FAILED` | — | stay alive, await manual `retry` — no automatic retry storm |

`backupd --once` always makes an immediate decision (ignores the "wait until
04:00" gate) — it exists for manual/canary invocation, not passive scheduling.

Historical note: before 2026-08-29 the schedule anchor was local midnight,
not 04:00 — see `HANDOFF.md` for the change record. Retention's
daily/weekly/monthly rollover boundaries (`app/util.py`'s `next_midnight`)
are a separate, unrelated calendar-day concept and are unaffected.

## Credentials location (changed 2026-09-11)

`config/credentials.env` is a **symlink** to
`/workspace/.pod-persist/secrets/osf-backup.env`, not a real file in this
tree. This is deliberate, post-incident: on 2026-09-11 this entire directory
was accidentally deleted while `backupd` was live, and the only copy of
production credentials (which lived here as a real file) was lost with it —
recoverable only because the AWS keys could be re-issued and the ntfy/
Healthchecks URLs were still known. Keeping the real file outside this tree
means deleting `_BACKUP` again cannot also delete its own credentials. Every
reader in this codebase (`app/storage.py`, `app/notify_ntfy.py`,
`app/healthcheck.py`, `app/notify_email.py`) opens `config/credentials.env`
by path and follows the symlink transparently — no code change was needed.

## Notifications

Three independent channels, deliberately isolated from each other and from
backup truth — a channel failing never changes a transaction's own
SUCCESS/FAILED phase, and one channel crashing never prevents another from
being sent (see `Runner._send_transaction_notifications`,
`_send_interrupted_notification`, `_ping_healthcheck`, each wrapping its
channel in its own try/except).

### ntfy.sh (primary daily push)

`app/notify_ntfy.py`'s `NtfyClient` publishes a concise phone push after
every scheduled transaction attempt (SUCCESS or FAILED) and on an operator-
requested interrupt — this is the "does this still look sane" channel,
meant to be glanced at on a lock screen, not the full report (see
`backupctl report`/`status` for that).

Configured via `BACKUP_NTFY_ENABLED` and `BACKUP_NTFY_URL` (env, falling
back to `config/credentials.env`, same precedence as every other credential
here). **The topic name IS the access secret** — ntfy.sh topics are
unauthenticated by default, so `BACKUP_NTFY_URL` must be a long, random,
unguessable topic (128-bit hex, `osf-backup-<32 hex chars>` by convention),
never a descriptive name. It is masked in every log line and must never be
written into logs/HANDOFF/docs in full — mask as `osf-***`.

Run `backupctl test-ntfy` to publish a test push without running a backup —
requires the operator to confirm it actually arrived on the phone, since a
2xx from ntfy.sh only proves the HTTP call succeeded, not that anyone is
subscribed to the topic.

### Healthchecks.io (independent dead-man switch)

Dead-man monitoring via Healthchecks.io. If the pod is dead, nothing on it can
report that — the missing ping is the signal. A `/start` with no success inside
the grace period also catches a *hung* run, not just a never-started one. This
is deliberately a *different* failure mode than ntfy: ntfy requires the code
to run far enough to build a report and make an HTTP call; Healthchecks fires
from Healthchecks' own servers on a **missing** signal, so it still works when
the pod is dead, backupd never woke, networking is down, or `notify_ntfy.py`
itself has a bug.

The URL is read from `BACKUP_HEALTHCHECKS_URL` and never written to config,
logs, reports, or the backup itself. Configure the check in the
Healthchecks.io UI as a cron-style check, `0 4 * * *`,
timezone `Australia/Melbourne`, with a **26-hour initial grace period**
(observed real run durations range roughly 9–24h; a tighter grace like 8h
will false-alarm on a normal long run). A cancelled/interrupted run
deliberately pings neither `/start`-completing endpoint (`/success` or
`/fail`) — it produced no snapshot, so the dead-man should stay hungry and
alert if the day ends without a real success.

### Email (optional, off by default)

After every scheduled transaction attempt (SUCCESS or FAILED), `Runner` can
also send a human-readable email to `BACKUP_EMAIL_TO` via
`app/notify_email.py`'s `EmailClient` — file count, protected size,
classification breakdown, top source roots, a diff against the previous
SUCCESS snapshot, deferred-active files, and the result (success email), or
the failed phase/resume phase/progress/error (failure email). All three
channels' report content comes from the same
`app.runner.build_transaction_report`, reading only the real
`transactions`/`scan_items` tables — nothing sampled or fabricated.

Configured entirely via env vars, falling back to `config/credentials.env`
for whichever aren't already in the environment (same precedence as the S3
credentials above): `BACKUP_EMAIL_ENABLED`, `BACKUP_EMAIL_TO`,
`BACKUP_SMTP_HOST`, `BACKUP_SMTP_PORT` (default 587), `BACKUP_SMTP_USERNAME`,
`BACKUP_SMTP_PASSWORD` (an app password, never the account password),
`BACKUP_EMAIL_FROM`. `BACKUP_EMAIL_ENABLED` unset (the default, and the
current production setting since ntfy became primary on 2026-09-08) makes
every send a documented no-op — email is opt-in, never required for a
backup to run or to be considered SUCCESS. The code is fully functional and
tested; it is simply disabled by configuration, not removed, in case it is
ever wanted again.

**Notifications must never control backup truth**: a send failure on any of
the three channels is recorded on the transaction row (`ntfy_status`/
`ntfy_attempts`/`ntfy_error`, `healthchecks_last_kind`/
`healthchecks_last_status`/`healthchecks_last_error`, or
`notification_status`/`notification_attempts`/`notification_error` for
email) — visible in `backupctl status` and flagged by `backupctl doctor` —
but never changes the transaction's own SUCCESS/FAILED phase. Bounded
retries (3 attempts, backoff) happen inside one send; a channel failure is
never retried by re-running the backup.

Run `backupctl test-email` to send `[OSF Backup TEST] Email notifications
working` without running a backup — recommended before re-enabling email for
unattended production use, so a misconfigured SMTP setup is caught by an
operator, not discovered on the first real transaction.

## Capacity

Capacity is tracked in a local ledger, not discovered by a failing upload.

| Threshold | Behaviour |
|---|---|
| 70% | warning in report |
| 80% | stop admitting discretionary outputs; critical/BTS continue |
| 90% | hard alarm — a critical/BTS upload that would cross it **fails visibly** |

A critical file is never silently skipped to stay under quota. Crossing the
hard threshold means the volume needs enlarging or retention needs changing,
and the run says so rather than reporting success.

## Local staging

**No ordinary backed-up file is ever retained locally under `_BACKUP` merely
because it was uploaded to the remote CAS.** `_BACKUP`'s persistent footprint
is code/config/state plus small, bounded, transaction-owned staging — never
a local duplicate of the protected set. This is a hard invariant, not a
best-effort target: it's what a 2026-08-29 incident (`state/blob_cache/`
growing to 174 GiB — 99.999% of it already durable on S3) was fixed to
prevent from recurring. See `app/runner.py`'s module-level comments on
`Config` and `Runner._upload`/`_cleanup_transaction_staging` for the full
mechanism; the summary:

- **Ordinary files upload directly from their source path.** No copy step
  exists in that path at all.
- **Persistent staging is limited to the few cases where generated bytes
  themselves matter to transaction consistency**: a SQLite `.backup()`
  snapshot (tens of MiB) or a git-rescue package, both under
  `state/staging/<txn_id>/`, deleted the instant their own upload succeeds
  (see `Runner._cleanup_staging_file`) — while still uploaded, they're
  `REQUIRED_FOR_RESUME`; the moment they're durably `UPLOADED` they're
  `NO_LONGER_NEEDED`. A `FAILED` transaction retains only whichever of these
  weren't uploaded yet — never purged wholesale on failure.
- **Everything else is ephemeral.** Manifest JSON, the catalog.db snapshot,
  and verify-phase sample downloads are all regenerable from current state
  on any resume, so they're built and consumed in one call via a `tempfile`
  scratch path (typically `/tmp`), never touching persistent staging.
- **A SUCCESS transaction sweeps its own staging directory** once every
  remote durability gate has passed (manifest committed, catalog uploaded,
  retention done) — `Runner._cleanup_transaction_staging`. Sweep failure is
  recorded as `local_cleanup_status`/`local_cleanup_error`/
  `local_cleanup_bytes_left` on the transaction row and surfaced by
  `backupctl status`/`staging-status`/`doctor` — it never turns a genuine
  remote SUCCESS into FAILED.

**Expected footprint:**

| State | Size |
|---|---|
| Idle (between transactions) | ~0.5–1 GiB (mostly `state.sqlite`) |
| During an active transaction | a few GiB at most, if git-rescue genuinely needs it |

It should **never** scale with the protected backup set or source media
size — if it does, that's the regression this section exists to catch.
There is deliberately no hard quota enforced on staging (legitimate
git-rescue's worst case isn't characterized yet), only warning thresholds.

**Operator tools:**

- `backupctl staging-status` — total persistent staging, broken down by
  transaction and category (sqlite/git_rescue/catalog/manifest/verify/
  unknown), flags staging owned by a `SUCCESS` transaction (stale — should
  have been cleaned) or with no matching transaction at all (unknown
  owner), and the oldest artifact's age. Also checks `state/blob_cache/`
  is empty — production code should never write there again.
- `backupctl staging-clean [--apply]` — deletes staging owned by `SUCCESS`
  transactions only (dry-run by default); never touches a `FAILED` or
  nonterminal transaction's staging, never touches unknown-owner staging
  without a human looking at `staging-status` first.
- `backupctl status` prints a `Local staging: <size>` line; `backupctl
  doctor` warns (never fails) above 5/10/25 GiB, and always warns on
  unknown-owner staging, staging owned by a `SUCCESS` transaction, or any
  `state/blob_cache/` content — ownership matters more than raw size, since
  a large in-progress git-rescue can be legitimate while a small leftover
  from a weeks-old `SUCCESS` is not. The daily email report includes the
  same figure as "Local backup staging".

## Known constraints (RunPod S3)

- Bulk `DeleteObjects` is **not supported** — objects are deleted individually.
- Files over 500 MB **require** multipart upload.
- Listing large/complex prefixes is slow and unreliable — the nightly run never
  recursively lists `blobs/`. It uses the local catalog and exact known keys.
- Network volume capacity is fixed.
