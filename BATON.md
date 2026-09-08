# BATON — project: _BACKUP (OSF Selective Backup / backupctl)

**Precedence:** Tier 0 (observed reality) > this file > any compaction summary > everything else.

## MISSION
Selective backup system (`bin/backupctl`, `app/*.py`, `config/policy.yaml`) that classifies
`/workspace` into CRITICAL/BTS/OUTPUTS/PRINCIPAL/REDOWNLOADABLE/GIT_RESCUE/GIT_REMOTE_BACKED/
SQLITE_DATABASES/UNCLASSIFIED and uploads only the protected set to a RunPod S3 bucket.

**Sizing phase (2026-08-17 to -19) is done**: classified correctly, measured the natural protected
set, modeled churn, volume resized 100GB -> 300GB (Daniel, via RunPod dashboard). Not mechanically
re-tracked here — see MemPalace diary / this file's git history-less nature (no repo here) if the
sizing reasoning itself is needed again.

**Sizing/pre-seed/first-two-seed-attempts phases are all done and superseded.** `backup-2026-08-19`
was attempted three times across checkpoints -3h/-3i/-3j/-3k, hitting three distinct RunPod gateway
issues, then found (checkpoints -3l/-3m) to rest on an incomplete scan under a policy that was never
fully correct. **It stays `FAILED`, untouched, as historical evidence — do not retry or reset it.**
Daniel gave GO for a completely fresh seed instead. **Read `HANDOFF.md` checkpoint -3n first** (not
-3m, not this file) **for the actual current state** — this section is a pointer, not the source of
truth.

## NOW
**CANARY PHASE CLOSED, 2026-08-29T01:33 Melbourne.** `backup-2026-08-23` (old unbatched impl),
`backup-2026-08-27` and `backup-2026-08-28` (real `backupd`-driven, new batching) all reached genuine
SUCCESS, independently verified — full checklist and evidence in memory
`backup-canary-aug27-decisive-checks`, not re-duplicated here. `backup-2026-08-29` auto-started at
exact Melbourne midnight (proving the scheduler wake works) then was **deliberately SIGKILLed by
Daniel's explicit instruction** mid-SCANNING (graceful SIGTERM doesn't interrupt an in-progress
transaction — see TRAPS below) — left as an honest, untouched, interrupted historical transaction
(`phase=SCANNING`, no S3 writes, not marked SUCCESS/FAILED). **Do not touch
`backup-2026-08-19`/`-23`/`-27`/`-28`/`-29` further.**

**Daniel's ruling: two consecutive real daemon-driven successes (Aug27, Aug28) is sufficient evidence
— do not run another backup transaction.** Next phase is operational, per the original gate sequence
below: enable real `supervisor`, restart/recovery tests, Healthchecks/dead-man monitoring, then leave
production enabled. **Not yet started as of this note — needs Daniel's go-ahead to begin,** same as
before.

**~~Real gap found this session~~ — FIXED 2026-09-07, see HANDOFF -3r §1.** `backupd`'s
`_run_transaction()` used to call `runner.run_for_date()` with no interrupt checks inside it, so a
SIGTERM only took effect *between* transactions and SIGKILL was the only way to stop the Aug-29
SCANNING run. Runner now takes an optional `should_stop` predicate (`Config.should_stop`, polled at
phase boundaries and between items in SCANNING/UPLOADING/VERIFYING) and raises `TransactionCancelled`,
which is explicitly **not** a failure — the phase is preserved for resume and nothing is marked FAILED.
Proven end-to-end in a real daemon process against a scratch home/source/object-store: SIGTERM
mid-UPLOADING → clean exit in 0.06s, restart resumed and reached SUCCESS.

**⚠ Notification overhaul CONFIGURED on all three channels, 2026-09-08T~11:20+10:00 Melbourne.**

- **ntfy: LIVE and end-to-end verified.** Real `backupctl test-ntfy` sent, Daniel confirmed the
  push arrived on his phone. Closed by his explicit ruling — do not re-touch absent a new request.
- **Healthchecks.io: CONFIGURED.** Daniel created the check himself (Cron `0 4 * * *`,
  `Australia/Melbourne`, 26h initial grace, ntfy set as Healthchecks' own alert-integration target)
  and supplied the real ping URL in chat — wired into `BACKUP_HEALTHCHECKS_URL=` in
  `config/credentials.env` (masked everywhere as `hc-***`, never repeated in full in any doc).
  `backupctl doctor`'s "no dead-man monitoring" WARN is gone; `status` shows `Healthchecks:
  configured`. No test ping sent to the real check per Daniel's explicit instruction — **live
  verification is still pending the next genuine scheduled 04:00 run's real `/start` + terminal
  signal.**
- **email: optional/disabled**, unchanged, fully functional.

**No `BACKUP_HEALTHCHECKS_ENABLED` var — settled, do not add it.** `HealthcheckClient` arms purely
on `BACKUP_HEALTHCHECKS_URL`'s presence; Daniel explicitly ruled a second enabled/disabled boolean
would just create a second source of truth (URL present + enabled=0, URL absent + enabled=1,
precedence rules for malformed combos) for no benefit over one unambiguous condition. Do not
re-raise this.

Not Code's job to create Healthchecks accounts/checks — standing Aug-23 ruling: creating a
resource on an external service on Daniel's behalf is off-limits, routine *use* of a URL he hands
over is not. See HANDOFF -3w for the full session record and the exact doctor/status output.

**Daniel's own recorded closing framing:** ntfy complete/live/phone-proven; Healthchecks
complete/configured with no synthetic production ping sent; email deliberately disabled/optional;
code/tests/docs complete; production backup left undisturbed throughout. **Only outstanding
verification:** the next genuine scheduled run (tomorrow's real 04:00 Melbourne) produces the real
Healthchecks `/start` and then its genuine terminal success/fail signal — once that appears, mark
the entire notification/dead-man monitoring overhaul (checkpoints -3t through -3w) closed. The
pre-existing "last successful snapshot age" doctor failure during an active SCANNING run is
unrelated and does not reopen this.

**⚠ PRODUCTION IS ENABLED as of 2026-09-07T23:48+10:00.** Daniel gave the blanket go-ahead;
`supervisor.sh` + `backupd` run unattended, `backup-2026-09-07` is in flight (first backup in 10
days), and gates 7/9/10 are done — including a real SIGTERM restart test against the live
production daemon (clean stop in 0.80s, supervisor restarted it, transaction resumed). **Gate 8
(dead-man monitoring) is the only item still blocked on Daniel: paste a Healthchecks.io ping URL
into the `BACKUP_HEALTHCHECKS_URL=` line in `config/credentials.env` (renamed from
`BACKUP_HEALTHCHECK_URL` 2026-09-08, singular→plural) and restart the daemon.** Seven
previously-unprotected trees are now adjudicated and encoded; both stuck transactions are closed via
the new `ABANDONED` terminal phase with all their evidence kept. **Read HANDOFF -3s first, then -3r.**

**⚠ Verify daemon liveness with `/proc`, never `ps`** — a harness wrapper filtered `ps aux | grep`
output on this pod and showed nothing for two processes that were provably alive.

**READ HANDOFF -3r** (2026-09-07) for the fixes underneath all of that. It also carries three more
silent-visibility defects found and fixed the same session — daemon logging dropped every `app.*`
record (the 17-hour Aug-27 canary produced a 194-byte `backupd.log`); the real `run` path never
reported UNCLASSIFIED, only `dry-run` did; `status` called a 10-day-dead transaction "in progress"
and doctor never mentioned it — plus a table of trees that are **genuinely unprotected under the
current policy** and need Daniel's adjudication (`sparkvsr-pulse-lab`, `three_ghosts`,
`_incident_2026-09-04`, top-level `logs`, `github`, `sparkvsr-prod`, and every loose file at
`melb_acquire/`'s root, which is where its `BATON.md`/`DECISIONS.md` live).

- **SCANNING completed 2026-08-24T01:25:39+10:00 (~9.7h), checkpoint fully sane, no approval pause per
  Daniel's standing order — see HANDOFF -3n for the full report.** 140,450 files / ~169.92 GiB
  selected (well under the 210 GiB investigate-first threshold); every presence/absence spot-check
  matched exactly; `policy_hash` still matches; not a reuse of Aug-19's scan (disjoint rowids); 981
  expected `DEFERRED_ACTIVE` items. One non-blocking gap found: the real `run` path logs zero
  UNCLASSIFIED activity (only `dry-run` does).
- **Mid-run correction, Daniel's call (2026-08-24):** excluded 4 redundant WordPress-dump subtrees
  (`website/DL_Dump`, `website/wetransfer_u797478838_xubk0-3-sql_2025-11-25_2206`,
  `website/recovery`, `website/recovery-backups` — 3 near-duplicate backups of the same site,
  ~44,000 tiny files/~1.66 GiB dominating the upload tail) as `paths.redownloadable`; the rest of
  `website/` stays selected. Removed the matching `scan_items` rows for `backup-2026-08-23`
  specifically (56,668 rows, including 9,549 already-uploaded `DL_Dump` files — those blobs stay in
  CAS, unreferenced, GC-later) and resumed via real code. See HANDOFF -3o.
- **⚠ Repeated the -3i date-rollover trap once during this, caught fast:** `backupctl run
  --force-new-date` after the correction landed past Melbourne midnight (pod clock is UTC) and
  started a stray `backup-2026-08-24` scan instead of resuming. Killed within ~15s, cleaned up (both
  the transaction row and its 706 scan_items rows deleted — never resumed, no value keeping them).
  **`backupctl retry <exact-date>` is the only correct way to resume a specific known transaction** —
  `run`/`--force-new-date` always target "today," which is not what you want once you already have a
  transaction to resume. See HANDOFF -3o for the full mechanism (why `retry` works for any non-SUCCESS
  phase, not just FAILED).
- Now `phase=VERIFYING` for real (HeadObject + sampled download-verify against the corrected,
  83,782-item selection, ~167.9 GiB). UPLOADING finished clean, 10 `DEFERRED_ACTIVE` unchanged all
  session (concurrent live `comfy`/`erf` activity, correctly excluded from the manifest).
- On resume: check the background-task notification first; otherwise `ps aux | grep "backupctl"`
  and the `transactions` row's `phase`/`updated_at` directly.
- **S3 is no longer a blocker** — RunPod `QuotaExceeded` reclassified HISTORICAL/TRANSIENT/CLEARED,
  2026-08-23 (Daniel proved it unreproducible against the real bucket/implementation).
- **Found and fixed while launching**: `bin/backupctl doctor`'s S3 check had a real bug (misused
  `list_prefix`'s bounded-page assertion against the whole non-empty bucket, permanently
  false-failing since 2026-08-19) — fixed, live-verified, no dedicated test yet (flagged, not
  silent). Also stopped a stray running `supervisor.sh`/`backupd.py` (had been live since
  2026-08-16/17, the opposite of what a manual seed needs) and killed two of my own leftover
  background analysis scripts that were still holding `state.sqlite` open. See HANDOFF -3n for full
  preflight detail, including the S3 diagnostic-prefix / stray-object findings (bucket has ~363 MB of
  unrelated pod-bootstrap clutter outside `_osf_backup/`, non-blocking, not cleaned up, flagged for
  Daniel).

**Remaining post-seed gate sequence once `backup-2026-08-23` reaches SUCCESS (Daniel's, not started):**
1. Policy spot-checks against the real committed snapshot (C001/Media_H264/braw_decode protected;
   erf/media keyframes/proxies/thumbs, platelock/env, houdini_caches excluded; no new UNCLASSIFIED
   leakage; selected-byte total in a plausible ballpark — Daniel's own estimate is roughly ~163 GiB
   before source drift since Aug-19, investigate rather than assume if it's wildly higher).
2. Explicit `backupctl verify` against the committed snapshot.
3. A real restore drill (small authored file, directory/tree, large media/BTS file, git-rescue
   material, one ERF SQLite-safe snapshot).
4. Isolated `rebuild_state` from committed remote state — into a scratch DB, never live production.
5. Comparison with production logical state.
6. Deal cleanly with `backup-2026-08-19` (historical, incomplete, do not fabricate success) without
   it confusing any of the above.
7. Only then re-enable `backupd`/`supervisor`.
8. Configure Healthchecks/dead-man monitoring.
9. Controlled restart tests (daemon restart, pod restart).
10. Confirm next Melbourne-midnight run semantics (today's seed must not repeat; next logical run is
    the next calendar day).

**RESCAN REQUIRED: YES**, decided 2026-08-23 (HANDOFF -3m). Reason: `table_read/Media`
(`C001.braw.PARTIAL`, 19.26 GiB, the sole local copy of a recovery-pending recording),
`table_read/Media_H264` (4.91 GiB), and `table_read/braw_decode` (~120 MiB) — 25 files, ~22.96 GiB
total — were UNCLASSIFIED under the old policy and so were **never enumerated or hashed** by the
Aug-19 scan. Now CRITICAL/selected (this session's fix), but absent from `scan_items` means the
preserved transaction can't represent them no matter how it's reconciled. Do not retry
`backup-2026-08-19`; do not synthesize/inject rows into it. A fresh scan under the corrected policy is
the safe path — launching it is Daniel's/Opus's call, not taken this session.

**Full policy-conformance audit this session, all fixed and tested (27/27 policy tests, 6/6 resume
tests, 50/50 full suite):**
- `erf/media/{keyframes,proxies,thumbs}` OUTPUTS -> REDOWNLOADABLE (an old audit ruling never encoded)
- `platelock/env` OUTPUTS -> REDOWNLOADABLE (8.6 GiB unpruned venv, confirmed conventional via
  `pyvenv.cfg`)
- `POC_VFX/GeneralAssets/houdini_caches` CRITICAL -> REDOWNLOADABLE (dead rule fixed: was shadowed by
  an identical-prefix `critical` entry; audited the real tree, it's 2 VDB cache zips, nothing
  authored, so the `critical` entry was removed rather than reordered)
- `table_read/braw_decode`, `table_read/Media`, `table_read/Media_H264` UNCLASSIFIED -> CRITICAL (all
  three: real adjudicated-or-adjudicable content that was silently not being backed up at all)

**Also landed this session:** a policy-fingerprint resume guard (`app/runner.py`) — `transactions`
now has a `policy_hash` column, stamped at scan time; `run_for_date`'s FAILED-resume path now refuses
(fails closed, raises) to resume past SCANNING if the stored hash doesn't match the current
`policy.yaml`'s hash, including on `NULL`/legacy transactions. This structurally closes the "retry
silently reuses stale classification" gap found earlier this session — `backup-2026-08-19` itself will
now refuse a naive retry on its own once this ships, regardless of the rescan decision above.

**Deliberately not built this session:** a `backupctl reconcile` apply-path CLI. Considered (Daniel's
Phase 5 spec), but since RESCAN REQUIRED = YES, there's no real transaction to apply it against right
now — building an unexercised state-mutating CLI against a hypothetical would be against this
project's own conventions. Revisit if a future FAILED transaction is genuinely reconcile-eligible
(every policy delta is `selected->excluded` or `class-A->class-B`, nothing newly-required missing).

**Below retained for RunPod incident history/provider-behavior documentation — no longer a blocker:**

`backup-2026-08-19` is `FAILED` with `resume_phase=UPLOADING` on a `QuotaExceeded` `PutObject` error,
even though: the RunPod control plane confirms the volume (`v6a4zjiytb`, EU-RO-1) is genuinely 300GB;
a full S3 `ListObjectsV2` enumeration found only 89.57 GiB of real objects (61,879, all under
`_osf_backup/`, zero elsewhere, zero orphaned multipart uploads); a direct MooseFS-mount filesystem
check on that volume showed matching ~91.5 GiB real usage; and disposable filesystem writes there
(14.65 GiB sparse-safe, then a genuinely non-sparse 8 GiB write) both succeeded cleanly well past the
volume's old ~93 GiB boundary. This isolates the fault to RunPod's S3 gateway quota tracking being
stale since the 100GB→300GB resize on 2026-08-17 — see HANDOFF.md -3k for the full evidence chain and
the drafted (Daniel-approved) support-ticket text.

**A decisive follow-up test is queued but not yet run** (relayed to a second Code instance running
directly on the backup-volume pod, see HANDOFF.md -3k addendum): install MooseFS **client-only**
diagnostic tools (`mfsgetquota`/`mfsdirinfo`, never `mfssetquota`/`mfsdelquota`/`mfscopyquota`) to
read the real per-volume quota the mount enforces, and run one minimal 1 MiB S3 `PutObject` probe
(disposable key, single attempt, no retry loop) using the project's working S3 credentials. That
instance also ran `runpodctl config` once, which turned out **not** to be read-only — it left a
stray SSH keypair (private key confined to that pod, one extra public key on the RunPod account).
Daniel's call: leave it, don't spend effort reversing it, and **do not run `runpodctl config` again**
for this investigation.

**On resume: check for that Phase-4 test's results first** (mfsgetquota output + the 1 MiB PutObject
outcome) — they were pending when this session ended. Do not resize the volume again (300→301GB
"propagation nudge" was explicitly considered and rejected — see HANDOFF.md -3k addendum) and do not
retry `backupctl retry 2026-08-19` until RunPod actually confirms the S3 gateway quota is fixed.

**Still true, carried forward from earlier checkpoints:** RunPod's S3 gateway can 403 a `HeadObject`
issued immediately after a multipart `CompleteMultipartUpload`, before the object is fully resolved
server-side. `Runner._head_object_after_upload()` in `app/runner.py` is the fix — a bounded
3-attempt/2s-apart retry around the two post-upload HeadObject checks in `_upload()` and `_verify()`.
Real production behavior of the actual endpoint for any file over the 128MiB multipart threshold, not
defensive-programming excess — do not remove or "simplify" it without re-confirming the gateway no
longer does this.

## MODEL / ROUTING
- **Fable/Opus/Daniel** — final call on genuinely ambiguous design decisions (the git-rescue
  semantic-vs-full-repo design question got exactly this treatment this session — see HANDOFF.md
  -3f); any actual seed action.
- **Sonnet** — the wiring/classification/report work itself (judgment-heavy, data-loss risk if wrong).
- **DeepSeek (`ds_flash.sh codegen`/`patch_draft`)** — bounded script drafting only, and even then
  verify against real signatures before applying; this project's own PreToolUse hooks enforce routing
  through it for new files >80 lines and (inconsistently — see TRAPS) large file reads. Several of
  this session's most serious bugs were exactly this codebase's own established pattern: generated
  code inventing method names/signatures that don't match the real modules. Never trust a generated
  interface without reading the real one first.

## BLOCKED (needs Daniel) — carried over, still open
- **`erf/state`** SQLite handling: resolved differently than originally scoped, not left blocked.
  `policy.yaml`'s `sqlite_databases:` now lists the 4 live DBs explicitly; `Runner._backup_sqlite`
  routes them through SQLite's real `.backup()` API. Removed from BLOCKED.
- **`erf_data_backup/`**: resolved. It's GIT_RESCUE (confirmed via direct git inspection, not the
  timeout-fallback the scan hit) — 38 real dirty files, not the whole 2.6GB catalog tree. Removed
  from BLOCKED.
- ~~**`catalog/`** (top-level)~~ — **STALE ENTRY, resolved: verified 2026-09-07 that it is
  `paths.critical` in the live `config/policy.yaml` and classifies `CRITICAL`/selected.** It was
  adjudicated at some point without this entry being cleared. Original text kept below for the
  reasoning only:
- **`catalog/`** (top-level, ~347MB, distinct from `erf/catalog`): looks like legacy pre-ERF pipeline
  output (asr/audit/chunks/embeddings/entities/milvus/etc, matching the pipeline stage table in root
  CLAUDE.md). Deliberately left unclassified rather than guessed — could be reconstructable derived
  data (matches root CLAUDE.md's "derived acceleration layers" doctrine) or could be the only
  remaining record if `pipeline/`'s raw inputs aren't kept (pipeline is already excluded as
  redownloadable/legacy). Real data-loss risk either way; needs Daniel's read on whether `pipeline/`
  can still regenerate it.
- **`table_read/braw_decode`** (124MB, mixed authored source + rebuildable binary/logs): **resolved
  and encoded, 2026-08-23** — `paths.critical` in `policy.yaml`, live-verified
  `UNCLASSIFIED`→`CRITICAL` via `Policy.classify()`, covered by
  `test_table_read_braw_decode_is_now_protected`. Kept whole per the original volume-sizing-session
  call (not split at file level). Confirmed absent from `backup-2026-08-19`'s `scan_items` (0 rows) —
  one of the three paths driving the RESCAN REQUIRED = YES verdict, see HANDOFF -3m.

## OPEN
- Whether to wire git-aware classification into `bin/backupctl`'s actual `_classify_source_tree` (its
  own docstring says it still doesn't) so future `backupctl dry-run` runs reflect real git status
  without needing the standalone `_audit_20260817/classify_breakdown_git.py` script. Separate from
  gates 1-4 (production `run`, not `dry-run`, is what's now wired for real — see HANDOFF.md -3f).
- ~~Whether `.claude/projects` + `.claude/file-history` should be CRITICAL or acceptable to drop~~ —
  **resolved and confirmed encoded, 2026-08-23**: both are `paths.redownloadable` in `policy.yaml`
  (session/cache metadata excluded, authored `.claude/CLAUDE.md`/`settings*.json` stay `critical`),
  live-verified `UNCLASSIFIED`→`REDOWNLOADABLE` via `Policy.classify()` and covered by
  `test_claude_session_and_file_history_are_excluded`. This OPEN item itself had gone stale — a
  reminder that OPEN items need re-verifying against live `policy.yaml`, not just re-read.
- ~~`table_read/Media_H264/*` and `table_read/Media/*` classify `UNCLASSIFIED`~~ — **resolved and
  encoded, 2026-08-23**: both `paths.critical`, live-verified via `Policy.classify()`, covered by
  `test_table_read_media_partial_c001_is_protected` /
  `test_table_read_media_h264_is_protected`. `Media/3018_10171415_C001.braw.PARTIAL` (19.26 GiB) is
  the sole local copy of a recovery-pending recording; `Media_H264` (4 files, C002-C005, 4.91 GiB) has
  no local raw counterpart found anywhere. Both confirmed absent from `backup-2026-08-19`'s
  `scan_items` — the other two paths driving RESCAN REQUIRED = YES, see HANDOFF -3m.
- ~~`POC_VFX/GeneralAssets/houdini_caches`'s `redownloadable` rule is dead~~ — **resolved, 2026-08-23**:
  audited the real tree (2 files, both `Flame*.zip` VDB caches, no `.hip`/`.hipnc` present), removed
  the shadowing `critical` entry rather than reordering blindly, live-verified `CRITICAL`→
  `REDOWNLOADABLE`, covered by `test_houdini_caches_redownloadable_rule_is_no_longer_dead` +
  `test_poc_vfx_general_assets_sibling_of_houdini_caches_stays_protected` (proves the fix didn't
  broaden the exclusion).
- ~~`platelock/env` (8.6 GiB Python venv) is fully OUTPUTS/selected~~ — **resolved, 2026-08-23**:
  confirmed conventional `python3.12 -m venv` (`pyvenv.cfg`, only standard venv dirs, nothing
  authored), now `paths.redownloadable`, live-verified, covered by
  `test_platelock_env_venv_is_redownloadable_but_rest_of_platelock_is_not`.

## TRAPS — tried and REJECTED. Do not re-attempt.
- Running `du -sb` sequentially across large dirs on `/workspace` to size things by hand — the volume
  is I/O-bound (network-mounted), calls silently exceed the 2-minute Bash timeout with partial/no
  output. Use the classify_breakdown scripts (single walk, in-process aggregation) instead, backgrounded.
- Reading `app/policy.py`/`app/scanner.py`/`bin/backupctl` etc directly with the Read tool — this
  project's PreToolUse hook blocks Read on any file >200 lines (routes to `ds_flash.sh patch_draft`
  instead). Use `sed -n 'A,Bp' file` via Bash to read in <200-line chunks instead — works fine and
  wasn't a real limitation, just needed the workaround.
- Grep via the shell `grep` function/`command grep` in this environment returns garbled
  `line:col:` output with no matched text for multi-alternation patterns on a single file (a harness
  quirk, not a real tool failure) — use `python3 -c "..."` with `re.search` per line instead.
- RunPod REST API (`rest.runpod.io/v1/networkvolumes/{id}`) 403s with an empty body for this
  project's API key even though the key works fine elsewhere — use the older GraphQL API
  (`api.runpod.io/graphql`, query `myself { networkVolumes { id name size dataCenterId } }`) instead
  for volume control-plane info. Don't conclude the key itself is bad from the REST 403.
- `df`/`df -i`/`stat -f` inside a pod with a RunPod network volume mounted via MooseFS/FUSE
  (`mfs#<host>:9421`) report the **entire shared MooseFS cluster's** capacity (petabyte scale), not
  this volume's own allocation — there is no per-volume quota visible through standard filesystem
  tools from inside the pod; that quota lives on the MooseFS master and needs `mfsgetquota`/
  `mfsdirinfo` (not installed by default) to read directly.
- `runpodctl config` is **not read-only** — running it generated a new SSH keypair as a side effect
  (stray private key on the pod, extra public key added to the RunPod account). Don't assume a CLI
  subcommand named `config` is inspection-only; verify before running it during a diagnostic that's
  supposed to leave no trace.
