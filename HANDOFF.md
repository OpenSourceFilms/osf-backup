# HANDOFF — OSF Selective Backup

**Updated:** 2026-09-08T~11:20+10:00 (Melbourne). **Read checkpoint -3w first** (Healthchecks.io
CONFIGURED with a real production ping URL from Daniel; notification overhaul is now fully
configured on all three channels — ntfy LIVE and end-to-end verified, Healthchecks CONFIGURED
pending its first genuine scheduled signal, email optional/disabled) — then -3v, -3u, -3t, -3s, -3r.
Production is running fine throughout all of this: `backup-2026-09-08` (today's real 04:00
scheduled run — first fully-scheduled run since re-enablement) is SCANNING as of this checkpoint,
untouched by any of the work below.
**Install root:** `/workspace/_BACKUP` (shared network volume `cmkgi43hcx`).
**Built from:** GPU pod `dokilqcrpplxs3` (now closed). **Runs on:** the
CPU-only pod `4784cl0vka11ge` — confirmed via RunPod API this session.

## -3w. Checkpoint 2026-09-08T~11:20+10:00 (Healthchecks.io CONFIGURED with a real production ping URL — notification overhaul configuration is now complete on all three channels; only proof left is tomorrow's real 04:00 run)

Daniel created the Healthchecks.io check himself (Cron, `0 4 * * *`, `Australia/Melbourne`, 26h
initial grace, ntfy set as its own alert-integration target on Healthchecks' side — so a
Healthchecks-detected dead-man alert also lands in the same phone app as the daily ntfy report)
and supplied the real production ping URL in chat. **Full URL is never repeated here or anywhere
else in this codebase — masked as `hc-***`, same rule as the ntfy topic**, both from here on.

**Wired in:**
- `config/credentials.env`: `BACKUP_HEALTHCHECKS_URL=<masked>` set (uncommented, real value). No
  `BACKUP_HEALTHCHECKS_ENABLED` var — `HealthcheckClient.__init__` (`app/healthcheck.py`) reads
  only the URL and arms purely on its presence, unlike ntfy/email which do have a separate
  `_ENABLED` flag. **Daniel explicitly ruled this stays as-is, do not add one**: a second boolean
  would create a second source of truth (URL present + enabled=0, URL absent + enabled=1,
  malformed combinations needing precedence rules) for no benefit over the current single
  unambiguous condition. Settled — do not re-raise this in a future session.
- `credentials.env` stays excluded/uncommitted (unchanged, pre-existing).

**Verified read-only, no pings sent (per explicit instruction — no test ping to a fresh production
check; its first real `/start` from the next scheduled run is the verification):**
- `backupctl doctor`: `PASS healthcheck configuration` (previously `WARN: ... no dead-man
  monitoring exists`, now gone entirely). `PASS ntfy configuration` unchanged. The only `FAIL` is
  the pre-existing, unrelated "last successful snapshot age" (today's `backup-2026-09-08` is still
  SCANNING, expected mid-run, not a regression from this change). No other doctor line changed.
- `backupctl status` → `Notifications:` block: `ntfy: enabled`, `Healthchecks: configured`,
  `email: disabled`. No unrelated status change.
- No full UUID or URL appeared in any command output.
- No backup transaction, `supervisor`, or `backupd` process touched or restarted to activate this
  — the running `backup-2026-09-08` SCANNING transaction is completely unaffected; the new env var
  takes effect the next time `HealthcheckClient()` is constructed (next daemon wake or CLI
  invocation), which is exactly the intended zero-disruption activation path.

**Wiring confirmed correct against Daniel's own spec (already built and tested, no code changes
needed this checkpoint):** `HealthcheckClient._endpoint_url` builds `<base>` (success),
`<base>/start`, `<base>/fail`; `Runner._ping_healthcheck` calls exactly those three in the right
places (`start` at real transaction begin, `success(body)` on genuine SUCCESS, `fail(body)` on
genuine FAILED), each isolated in its own try/except so a Healthchecks transport failure stays
non-fatal to backup truth — this exact property has direct test coverage
(`test_healthcheck_crash_does_not_stop_run_for_date_or_ntfy`,
`test_record_healthchecks_result_none_is_recorded_as_failed_client_raised`, both in
`tests/test_runner_ntfy_and_healthchecks_notification.py`).

**Notification overhaul status, all three channels (Daniel's own recorded framing, 2026-09-08):**
- **ntfy: complete, live, phone delivery proven.**
- **Healthchecks: complete/configured, no synthetic production ping sent.**
- **Email: deliberately disabled/optional.**
- **Code/tests/docs: complete.**
- **Production backup: left undisturbed** throughout every step of this whole overhaul.
- **Only outstanding verification:** the next genuine scheduled run (tomorrow's real 04:00
  Melbourne) produces the real Healthchecks `/start` and then its genuine terminal success/fail
  signal. **Once that appears, the entire notification/dead-man monitoring overhaul is closed** —
  a future session should check for it and, if present, mark this whole thread (checkpoints -3t
  through -3w) done rather than re-deriving status from code.

The pre-existing "last successful snapshot age" doctor failure while `backup-2026-09-08` is
actively SCANNING is a separate, unrelated matter (production run timing, not notifications) and
does not reopen this workstream.

**Nothing above blocks or was allowed to touch production** — same standing rule as every
checkpoint in this file since -3t.


## -3v. Checkpoint 2026-09-08T~11:05+10:00 (ntfy CLOSED by Daniel's explicit ruling after a real live test; only Healthchecks.io remains, blocked on Daniel by design)

**Live test, run for real (not a rehearsal):** `./bin/backupctl test-ntfy` against the production
`config/credentials.env` topic. Result: `ntfy test: sent (attempts: 1)`, exit code 0, no retries
needed (succeeded on the first HTTP attempt), no error, no backup transaction touched, no
Healthchecks ping sent (confirmed by reading `_cmd_test_ntfy` — it calls only
`NtfyClient.test_notification()`), full topic URL never printed in the command's output. Message
sent was the client's own built-in canned test copy (title `OSF Backup TEST`, body `ntfy
notifications are working from the RunPod backup system.`) — happened to match verbatim what
Daniel asked for, so no code changes were needed to run the test he specified.

**Daniel independently confirmed the push arrived on his phone** ("NTFY received"), then gave an
explicit ruling: **"ntfy is now fully proven end-to-end. I'd mark that workstream done."** Do not
re-open it absent a new request — see the NOW block in `BATON.md` for the same ruling landed
there.

**What's left is only Healthchecks.io**, and Daniel confirmed the plan himself (citing
healthchecks.io's own docs): a Cron check, `0 4 * * *`, `Australia/Melbourne`, **26h initial
grace** — his own reasoning: the longest completed run so far is ~23.9h, so anything much below
24h risks false alerts; tighten after a few weeks of real 04:00 run data. He is creating this
check himself in the Healthchecks.io UI — **not Code's job, per the standing Aug-23 ruling: creating
an account/resource on an external service on Daniel's behalf is off-limits; routine use of a URL
he hands over is not.** The receiving side is already built and test-covered exactly as he
specified the wiring (`<URL>/start` on begin, `<URL>` on genuine SUCCESS, `<URL>/fail` on genuine
FAILED) — see `app/healthcheck.py`'s `HealthcheckClient._endpoint_url` and
`app/runner.py`'s `Runner._ping_healthcheck`, both covered by
`tests/test_runner_ntfy_and_healthchecks_notification.py::test_healthcheck_endpoint_url_construction`.

**When Daniel supplies the ping URL, the only remaining action is:**
1. Paste it into `BACKUP_HEALTHCHECKS_URL=` in `config/credentials.env` (no code changes needed —
   the client already reads this exact var name, renamed from `BACKUP_HEALTHCHECK_URL` at -3t).
2. Confirm `backupctl doctor` stops WARNing "no dead-man monitoring exists".
3. Do **not** send a test success/fail ping to the real production check to verify it (per the
   original -3t spec's own section 15) — a fresh Healthchecks check will naturally get its real
   `/start` from the next scheduled 04:00 run; that first real ping is the verification.
4. Once confirmed, this closes the entire notification-layer overhaul (-3t through here) and the
   spec-section-17 final report-back (checklist item 8, still open) can finally be composed.

## -3u. Checkpoint 2026-09-08T10:50+10:00 (ntfy + Healthchecks overhaul — items 1/2/4/6 from -3t's checklist done; items 3/5/7/8 still need Daniel or depend on those)

Resumed directly from checkpoint -3t's "IN PROGRESS / NOT YET DONE" list after the context clear.
Production untouched throughout (`backup-2026-09-08` reached the same state it was already in).

**Done this session:**

1. **Status-output consolidation (item 1).** Removed the old inline
   `notification: {status} (attempts: ...)` line from `_cmd_status` (`bin/backupctl`) and added the
   matching "Last email notification" line into `_print_notification_status`'s bottom
   `Notifications:` block, next to the ntfy/Healthchecks lines it already had. One block, one place,
   per channel. Confirmed live: `backupctl status` now prints a single consolidated block.
2. **ntfy topic generated (item 2, partially).** `secrets.token_hex(16)` prefixed `osf-backup-`,
   stored as `BACKUP_NTFY_URL`/`BACKUP_NTFY_ENABLED=1` in `config/credentials.env`. Confirmed live
   via `NtfyClient()`: `enabled=True`, valid topic URL, no misconfiguration; `backupctl
   status`/`doctor` both render it correctly read-only. **The URL was given to Daniel once in chat
   this session, per spec — never write it in full anywhere again, mask as `osf-***`.** Daniel still
   needs to subscribe in the phone app before `backupctl test-ntfy` is run (item 5, still open).
3. **Test suite (item 4) — substantially built, from 218/223 to 240/245 passing, 0 failed.**
   Drafted via `ds_flash.sh patch_draft` (full `app/notify_ntfy.py`, `app/healthcheck.py`, the
   relevant `app/runner.py` methods, and two existing test files piped in as style templates — the
   >200-line read/write hooks require this route, obeyed rather than routed around). Validated by
   content (real symbol/line grounding checked before trusting it, per the DeepSeek-silent-timeout
   trap), then run for real: one genuine bug found and fixed (a test reused the same sqlite file
   across a 3-case loop, colliding on `(txn_id, path)` — fixed by giving each case its own tmp
   subdir). Two more tests hand-added directly (not through DeepSeek — small, targeted) after
   inspecting `Runner._ping_healthcheck`'s exact try/except behavior:
   - `tests/test_notify_ntfy.py` (14 tests) — priority/tag mapping per notification kind,
     success/failure/interrupted payload rendering, `_mask_topic_url` never leaks the full topic,
     disabled/misconfigured no-op paths make no HTTP call, bounded 3-attempt retry with linear
     backoff never raises.
   - `tests/test_runner_ntfy_and_healthchecks_notification.py` (10 tests) — ntfy+email isolation
     under every combination of one/both failing (phase always unaffected), an ntfy client
     exception doesn't stop email or the Healthchecks ping in a real `run_for_date`, a Healthchecks
     client exception (not just a failed result) doesn't stop `run_for_date`, ntfy-only firing on
     `_send_interrupted_notification` (Healthchecks correctly gets no ping), `_record_notification_result`
     writing the right channel's columns without touching the other's, `_record_healthchecks_result`'s
     `None`-result handling, `HealthcheckClient` start/base/fail URL construction.
   - `tests/test_build_transaction_report_fields.py` (3 tests) — `error_count` counts real non-null
     errors (not assumed zero), `total_selected_bytes` sums correctly, both present/typed with zero
     items.
   - **Not done:** a dedicated `backupctl test-ntfy` CLI-level test (no existing precedent for
     testing the CLI commands themselves in this codebase — `test-email` has none either; the
     underlying `NtfyClient` behavior it wraps is fully covered above). Left as a minor gap, not
     blocking.
4. **Docs (item 6) — done.** `README.md`'s old "Monitoring"/"Email notifications" sections replaced
   with a single "Notifications" section covering all three channels (ntfy primary, Healthchecks
   independent dead-man, email optional/off-by-default) and the isolation guarantee. `docs/DEPLOY.md`
   gained a renumbered step 7 "ntfy push notifications" between Healthchecks (step 6, now says 26h
   grace, not 8h — item 7 folded in here) and email (step 8, now marked optional); the step-0
   prerequisites table adds `BACKUP_NTFY_URL`/`BACKUP_NTFY_ENABLED` as not-optional and demotes the
   email row to optional, and the verification snippet's env-var list is updated
   (`BACKUP_HEALTHCHECK_URL`→`BACKUP_HEALTHCHECKS_URL`, `BACKUP_NTFY_URL` added). `BATON.md`'s NOW
   block rewritten to point here; its stale `BACKUP_HEALTHCHECK_URL=` reference (singular, pre-rename)
   fixed to the plural name.

**Still open, needs Daniel or depends on the above (see -3t for full original detail):**

- Item 3: Healthchecks.io ping URL — still not supplied.
- Item 5: live ntfy test — blocked on Daniel subscribing to the topic given to him in chat, then
  `backupctl test-ntfy`, then his phone confirmation.
- Item 8: final spec-section-17 report-back — still correctly premature; compose only once item 5
  (and item 3, if Daniel supplies it this pass) are actually done.

**Nothing above blocks production** — same as -3t.

---

## -3t. Checkpoint 2026-09-08T~09:00+10:00 (ntfy + Healthchecks notification overhaul — MID-IMPLEMENTATION, paused for a context clear, NOT tested end-to-end yet)

**Daniel's instruction (2026-09-08), verbatim intent:** replace the email-report design with
**ntfy.sh as the primary daily human-visible push notification**, keep **Healthchecks.io as the
independent dead-man switch**, and demote email to optional/disabled. He gave a detailed spec
(topic-entropy requirements, message formats, priority levels, retry policy, doctor/status
surfacing, a full test matrix, live-test sequencing). Full spec text is in the conversation, not
duplicated here — this checkpoint records what's actually been built against it.

**Why paused here:** a context clear was requested mid-task. Everything below is real, applied,
and test-green, but the work is genuinely incomplete against the spec — see the checklist at the
end. Do not treat this checkpoint as "done, move on."

### What's built and verified (218/223 tests passing, 0 failed, same as before this task started)

1. **Shared primitives consolidated into `app/healthcheck.py`** (the one module with no dependency
   on any other notifier, so both transports can import from it without a cycle):
   - `NotificationResult` dataclass moved here from `app/notify_email.py` (which now re-exports it
     — `from app.notify_email import NotificationResult` still works, no test breakage).
   - `format_duration(start_iso, end_iso)` moved here from `notify_email._duration` (same logic,
     public name, shared by both email and ntfy formatters).
2. **`HealthcheckClient` (app/healthcheck.py) reworked:**
   - Env var renamed `BACKUP_HEALTHCHECK_URL` → `BACKUP_HEALTHCHECKS_URL` (matches spec and the
     product's actual name). Safe clean rename, not a migration — the old var was never actually
     configured in production (still commented/empty in credentials.env when this started).
   - `start()`/`success()`/`fail()` now **return a `NotificationResult`** instead of `None` (still
     never raises — a transport failure or non-2xx response comes back as
     `status="failed"`/`error=...` rather than swallowed silently). This is what makes the new
     `healthchecks_last_*` tracking possible.
   - `config/credentials.env`'s commented line renamed to match (`# BACKUP_HEALTHCHECKS_URL=`).
   - `tests/test_healthcheck_credentials_fallback.py` renamed throughout (all 5 tests still pass).
3. **New `app/notify_ntfy.py`** — `NtfyClient`, modeled directly on `EmailClient`/`HealthcheckClient`:
   - Config: `BACKUP_NTFY_ENABLED`, `BACKUP_NTFY_URL` (env, falling back to credentials.env, same
     pattern as every other credential in this project).
   - Publishes via ntfy's **JSON API** (POST to server root with `topic` in the body), not the
     header-based form — avoids header-encoding pitfalls for a title/message this code doesn't
     fully control the charset of.
   - `send(title, message, *, priority, tags)` — bounded 3-attempt retry with linear backoff, never
     raises, returns `NotificationResult`.
   - `success(report)` / `failure(report)` / `interrupted(txn_id, phase, reason)` /
     `test_notification()` — compact formatters (`format_success_notification`,
     `format_failure_notification`, `format_interrupted_notification`), 3-8 lines each, pulling
     from the same `build_transaction_report()` dict the email formatter already uses. Priorities:
     success=default, failure=urgent, interrupted=high, test=default.
   - Topic masked in every log line (`_mask_topic_url` — first 4 chars + `***`, same treatment as
     the Healthchecks UUID and S3 keys).
4. **`build_transaction_report()` (app/runner.py) extended:** added `error_count` (real count of
   scan_items with a non-null `error`, not assumed-zero) and `total_selected_bytes` (denominator
   for the failure notification's "X / Y GiB" line). Both additive, backward-compatible with the
   existing email formatter.
5. **Runner wiring (app/runner.py):**
   - `Config.ntfy: Any = None` field added, same isolation contract as `email`/`healthcheck`.
   - New `transactions` columns (migration-guarded, additive): `ntfy_status`, `ntfy_attempts`,
     `ntfy_error`, `healthchecks_last_kind`, `healthchecks_last_status`, `healthchecks_last_error`.
     **Deliberately did NOT rename** the pre-existing `notification_status`/`notification_attempts`/
     `notification_error` triple (that's email's, has real historical rows from Aug/Sep
     transactions) — documented in the migration comment as "EMAIL's, kept under its original
     name."
   - `_send_transaction_email` renamed → **`_send_transaction_notifications`**: builds the report
     once, then calls ntfy and email **each in its own try/except** so one channel's crash can
     never silence the other. (Every call site and test double referencing the old name was
     updated — `tests/test_transaction_cancellation.py` had two.)
   - New `_send_interrupted_notification(txn_id, reason)` — ntfy-only, fired from the
     `TransactionCancelled` handler in `run_for_date`. Healthchecks still gets **no ping** on
     cancellation (unchanged from the 2026-09-07 design) — a cancelled run produced no snapshot, so
     the dead-man should stay hungry.
   - New `_ping_healthcheck(kind, body)` — isolates each of start/fail/success in its own
     try/except and returns the result (or None on an exception) for recording.
   - New `_record_healthchecks_result(txn_id, kind, result)` — persists which of
     start/success/fail was last sent and whether it transported. A `None` result (the healthcheck
     call itself raised) is recorded as `status="failed", error="healthcheck client raised"` rather
     than silently skipped.
   - `_record_notification_result` now takes a `channel` parameter (`"email"`/`"ntfy"`) and writes
     to the correct column triple.
6. **`app/wiring.py`:** `build_runner()` now also constructs `NtfyClient()` and passes it as
   `Config(ntfy=...)`.
7. **`bin/backupctl` CLI:**
   - New `backupctl test-ntfy` command (mirrors `test-email` exactly) — publishes a real test
     notification, reports HTTP result, tells the operator to confirm the phone push arrived.
   - `backupctl status` gained a `Notifications:` block: enabled/disabled for ntfy, configured/not
     for Healthchecks, enabled/disabled for email, plus the latest transaction's last ntfy result
     and last Healthchecks ping result (kind + status + error if any). Function
     `_print_notification_status()`.
   - `backupctl doctor`: fixed a **real pre-existing bug** while touching this code —
     `check_healthcheck_configuration` was checking `os.environ.get("BACKUP_HEALTHCHECK_URL")`
     directly instead of the client's `.url`, so it would print the "no dead-man" WARN even when
     the URL WAS configured via `credentials.env` (the file-fallback path). Now checks
     `client.url`. Added a parallel `check_ntfy_configuration` (config-presence only, never
     publishes — a doctor-triggered send would be noise, and for Healthchecks specifically would
     falsify the real dead-man state). Extended `check_last_notification_status` (renamed doctor
     label: "last transaction's notifications") to check **both** `ntfy_status` and
     `notification_status` (email), not email alone.
8. **Email demoted per spec:** `BACKUP_EMAIL_ENABLED=1` (which I'd set earlier this session while
   pursuing the Gmail-password path, before Daniel redirected to ntfy) was **reverted to commented
   out**. Email code is untouched/still fully functional, just off by default — exactly "disable by
   configuration, don't delete tested code" per the spec.
9. **Live-verified (read-only, no sends):** `backupctl status` and `backupctl doctor` both run
   clean against the real production state DB with the new code — confirmed the Notifications block
   renders correctly (`ntfy: disabled`, `Healthchecks: not configured`, `email: disabled`) and
   doctor's new checks pass/note correctly. **No real ntfy or Healthchecks send has been attempted
   yet** — no topic has been generated, no URL configured.

### IN PROGRESS / NOT YET DONE — pick up exactly here

1. **Was mid-edit when interrupted:** `bin/backupctl`'s top-of-status inline email notification
   line (`"  notification: {status} (attempts: ...)"`, printed directly under "Latest transaction:")
   is now redundant-ish with the new bottom-of-status `Notifications:` block, but NOT a bug — just
   slightly inconsistent placement (email's last result prints in one place, ntfy/Healthchecks's in
   another). Checked: no existing test asserts on that inline line's exact text
   (`test_backupctl_status.py` has zero matches for "notification"), so it's safe to consolidate
   into `_print_notification_status()` for a cleaner single block — **do this next**, it's a small
   cleanup, then re-run the full suite.
2. **Generate the ntfy topic.** Not done yet. Use `secrets.token_hex(16)` (128 bits) prefixed
   `osf-backup-`, per spec section 1 (no guessable names — the public ntfy.sh topic name IS the
   access secret). Store as `BACKUP_NTFY_URL=https://ntfy.sh/<topic>` and `BACKUP_NTFY_ENABLED=1`
   in `config/credentials.env` (already `hard_exclude`d from backup, confirmed). **Report the full
   URL to Daniel exactly once in chat** so he can subscribe in the phone app — never write the full
   topic into logs/HANDOFF/docs after that (mask it, same as everywhere else in this codebase).
3. **Configure Healthchecks.io** — still needs Daniel to supply a real ping URL (spec explicitly
   says implementation may complete without it; `status` must say "not configured", which it
   correctly does right now). When supplied: paste into `BACKUP_HEALTHCHECKS_URL` in
   credentials.env. Per spec section 15, do NOT send a fake success ping to the real production
   check to test it — use a separate temporary Healthchecks check or an explicitly agreed test with
   Daniel.
4. **Comprehensive test suite per spec section 13 — NOT WRITTEN YET.** Only ad-hoc manual/live
   checks have been done so far (functional but not committed as tests). Needed:
   - ntfy: success payload rendering, failure payload rendering, priority/tag values, deferred/
     unclassified figures present, bounded retry (3 attempts, backoff), permanent ntfy failure is
     non-fatal to a SUCCESS transaction, an ntfy client exception is non-fatal, `test-ntfy` command
     behavior (disabled/misconfigured/sent paths).
   - Healthchecks: `/start`/base/`/fail` URL construction (already covered historically? — verify,
     may need new tests for the NotificationResult-returning behavior specifically), report-body
     content, transport failure non-fatal, `_record_healthchecks_result`'s None-result handling.
   - Combined/integration: ntfy succeeds + Healthchecks fails, ntfy fails + Healthchecks succeeds,
     both fail — in every case assert the transaction's own phase (SUCCESS/FAILED) is completely
     unaffected. This is the single most important property in the whole spec and has NOT been
     explicitly tested yet (only informally true by code inspection — the try/except isolation is
     real, but nobody has written the test that proves it under combined failure).
   - `format_interrupted_notification` / `_send_interrupted_notification` — zero tests exist yet.
   - `build_transaction_report`'s new `error_count`/`total_selected_bytes` fields — zero direct
     tests yet (only exercised indirectly through the manual live checks above).
5. **Live ntfy test (spec section 14)** — blocked on step 2. Sequence once the topic exists: give
   Daniel the URL → he subscribes in the app → run `backupctl test-ntfy` → require HTTP 2xx → **he
   confirms the phone push arrived**. Do not run a real backup just to test this.
6. **Docs — NOT updated yet.** `README.md`, `docs/DEPLOY.md`, and this file's/`BATON.md`'s
   production-notification-design sections still describe (or don't mention) the old
   email-primary/Gmail-app-password design. Needs: ntfy = primary daily push, Healthchecks =
   independent dead-man, email = optional/disabled, plus the topic-entropy warning from spec
   section 16. **Remove the Gmail App Password from production prerequisites** — no SMTP credential
   should be required for production readiness (spec section 11).
7. **`backupctl doctor`'s Healthchecks grace-period guidance** — spec section 9 wants documentation
   (not code) that a Healthchecks cron-style check should be configured `0 4 * * *` /
   `Australia/Melbourne` with a **26-hour initial grace** (observed run durations range ~9-24h;
   do NOT use a tighter grace like 8h). This is operator-facing setup instructionthat belongs in
   docs/DEPLOY.md (item 6 above), not code — Daniel configures the grace period in the
   Healthchecks.io UI himself when he creates the check.
8. **Final report-back per spec section 17** — not yet composed (topic URL, enabled states, test
   results, doctor/status output, test pass/fail counts, docs-updated confirmation, a plain
   YES/NO on "production notification ready"). Compose this only once steps 2-6 are actually done —
   sending it now would be premature per the spec's own "report back" framing (it asks for real
   results, not a status update).

**Nothing above blocks production.** The 04:00 backup schedule, `backupd`/`supervisor`, and the
existing (disabled) email code are all fully functional throughout. This is purely additive
notification-layer work.

## -3s. Checkpoint 2026-09-07T23:55+10:00 (PRODUCTION ENABLED — policy adjudications encoded, both stuck transactions closed, gates 7/9/10 done, two more real guard bugs found and fixed during the live restart test)

Daniel's instruction: *"Go ahead, fix everything, get it all running."* Everything below was done
under that authorisation. **211 tests pass** (up from 172 at the start of this checkpoint's work).

### Gate 7 — unattended scheduling is ON
`bin/supervisor.sh` (pid 98654) and `app/backupd.py` are running. `backup-2026-09-07` was created
and began SCANNING at 23:48:52 Melbourne — the first backup in 10 days. `backupctl doctor` now
reports **PASS daemon and supervisor status** and **PASS no abandoned in-progress transaction**;
the remaining FAIL (last successful snapshot age) is correct and clears when this run commits.

**Verify liveness with `/proc`, not `ps`.** This session's `ps aux | grep` returned *nothing* for
two processes that were demonstrably alive (confirmed by reading `/proc/<pid>/cmdline` and by the
flock being held) — a harness wrapper filters that output, which is the same class of quirk this
file's TRAPS already record for `grep`. I briefly concluded the daemon had died on that evidence.
It had not.

### Gate 9 — controlled restart test, run for real against production
With the live transaction in SCANNING, `SIGTERM` was sent to the running `backupd`:
- exited **rc=0 after 0.80 s**, mid-phase — before 2026-09-07's cancellation fix this required
  SIGKILL and could not be done at all;
- `transactions.phase` left at `SCANNING`, `error` NULL, nothing marked FAILED;
- `supervisor.sh` logged the exit, waited its 10 s, and restarted the daemon on its own;
- the new daemon read the phase and resumed the same date's transaction.
Deliberately run while the scan was ~90 s old so the restart cost minutes, not hours.

### Gate 10 — next-run semantics confirmed
`backupctl status` reports the next scheduled run as 2026-09-08T04:00:00+10:00. Today's transaction
is in flight; when it reaches SUCCESS, backupd's main loop sleeps to that time rather than repeating
(the SUCCESS branch), and `run_for_date` no-ops on an already-SUCCESS date regardless.

### Gate 8 — STILL BLOCKED ON DANIEL (the only one)
There is no dead-man switch. `BACKUP_HEALTHCHECK_URL` is unset, and creating a Healthchecks.io
account would mean generating new credentials on an external service on Daniel's behalf — off-limits
without him asking. **What was done to make it a one-line job for him:** `app/healthcheck.py` now
falls back to `config/credentials.env`, the file that has always shipped a commented
`BACKUP_HEALTHCHECK_URL=` line inviting exactly that. Until now the client read `os.environ` alone,
so pasting the URL where the file told you to would have configured *nothing* — a dead-man that
silently never armed. Uncomment the line, paste the ping URL, restart the daemon. 5 tests cover it,
including that the value is never logged in full (the UUID is the whole credential).
Email notification is in the same state: `BACKUP_EMAIL_ENABLED` and `BACKUP_SMTP_PASSWORD` are both
commented out, so a Gmail app password from Daniel would enable the second channel.

### Policy adjudications — encoded (this had to land BEFORE the scan started, and did)
All seven trees from -3r §6 are now ruled, protective-direction, with the rebuildable bulk carved
back out by longest-prefix-wins:
- **CRITICAL**: `melb_acquire` (tree root — this is the one that was really biting: its subdirs were
  ruled long ago but nothing covered its root, where `BATON.md`/`DECISIONS.md`/audit reports live),
  `melb_acquire/ASIC_bulk` (same class as the already-CRITICAL `ABN_bulk` sibling),
  `sparkvsr-pulse-lab`, `sparkvsr-prod`, `three_ghosts`, `_incident_2026-09-04`, and `github`'s loose
  files only (`BATON.md`, `CLAUDE.md`, `LICENSING_RESEARCH_BRIEF_2026-07-22.md`, `licensing.txt`,
  `.claude`, `runpod_renewal`).
- **OUTPUTS** (protected, capacity-skippable): `sparkvsr-pulse-lab/runs` (3.16 GiB), `three_ghosts/media`
  (the 3 frozen .mp4s S03_FROZEN.sha256 attests to), `sparkvsr-prod/output`, top-level `logs`.
- **REDOWNLOADABLE**: `melb_acquire/{.venv,__pycache__,.pytest_cache,.rtfm,.rtk,.duckdb_tmp}`,
  `github/.rtfm`, `sparkvsr-prod/{models,cache}`.
- **Deliberately NOT ruled**: anything inside `github/*` repo mirrors. All 14 are git repos with
  GitHub remotes, and **path rules outrank git classification** — a blanket `github` rule would have
  forced full-repo backups of material already safe on GitHub.
24 new parametrized regression assertions plus two structural tests (that the repo mirrors stay
unruled, and that `melb_acquire`'s new root rule didn't disturb the five rulings already under it).
Every pre-existing ruling was re-verified unchanged. `policy_hash` is now `b7ee853b0d2df920…`, which
differs from Aug-27/28's — expected, not a defect.

### Both stuck transactions closed — new `ABANDONED` terminal phase
`backup-2026-08-20` (18 days at SCANNING) and `backup-2026-08-29` (10 days) are now `ABANDONED`,
each with an explicit reason recorded in the row. **Nothing was deleted** — all 42,866 + 706
`scan_items` rows remain as evidence, including Aug-29's exact-midnight creation, which is the
scheduler-wake proof.
`FAILED` was the wrong word for these (they did not fail, and for a date newer than the last SUCCESS
it would have blocked `backupctl run` forever via the older-FAILED guard), so `Phase.ABANDONED` was
added as a distinct terminal state, written only through `Runner.abandon_transaction()`
(single-writer) and exposed as `backupctl abandon <date> --reason "..."`. It refuses to rewrite
anything already terminal — a SUCCESS must never be rewritten and `backup-2026-08-19` is historical
evidence in its own right — and `run_for_date` now *raises* rather than silently no-ops if asked to
resume an abandoned transaction. `backupd`'s main loop treats it as terminal (without that branch it
would have spun with no sleep). 6 tests.

### Two more real bugs, both found by actually running the thing
1. **The daemon-active guard false-positived on an unrelated process.** `DAEMON_PROCESS_PATTERNS`
   was `("supervisor.sh", "backupd.py")`, matched against full command lines — so
   `/workspace/.pod-persist/bin/rtfm-supervisor.sh --daemon`, which is RTFM's supervisor and always
   running on this pod, matched. Every `backupctl run`/`retry`/`abandon` was refused, telling the
   operator to kill a process they must not touch. It blocked my first two abandon attempts.
2. **…and false-negatived on the real one.** Fixing (1) with absolute paths introduced the dangerous
   direction: the live supervisor's cmdline is whatever was typed — `bash bin/supervisor.sh`,
   relative — which matches no absolute-path pattern. A guard that misses a live daemon permits the
   two-writer corruption it exists to prevent.
   **Fix:** the guard now consults the **flock** on `logs/supervisor.lock` / `state/backupd.lock`
   first — the same lock the daemon itself holds, immune to naming entirely — and uses the
   command-line scan only to enrich the message with pids. `_lock_is_held` was lifted out of
   `doctor` to module level so both callers share one liveness rule instead of two copies that
   drift. Verified live: with the daemon running, `backupctl retry` correctly refuses and names both
   held locks plus the real backupd pid. 4 new tests, including the exact rtfm-supervisor cmdline.

### Still open, not blocking
- Gate 8 (above) — needs Daniel.
- ~9,302 remote orphan blobs from the Aug-23 mid-run correction. Daniel already ruled this a
  reclamation-completeness gap, not a safety issue; GC is structurally blind to them because they
  have no local `blobs` rows. Unbuilt by design.
- `backup-2026-08-19` remains FAILED and untouched, as instructed.
- 30 MiB of staging still held by that Aug-19 transaction.

## -3r. Checkpoint 2026-09-07T23:40+10:00 (gate-9 blocker fixed and proven in a real process; three more silent-visibility defects found and fixed; a real unprotected-data gap found in current policy — nothing live was started, no transaction was run, production state untouched)

**Standing state, unchanged and needing Daniel:** last SUCCESS is `backup-2026-08-28`. Nothing has
been backed up for **10 days**. `backupd`/`supervisor` remain OFF. Gates 7-10 (re-enable daemon,
Healthchecks, restart tests, next-midnight semantics) have still not been authorized.

### 1. `backupd` could not be stopped mid-transaction — FIXED, and proven end-to-end
The gap recorded in BATON's NOW section (`_run_transaction` calls `run_for_date` with no interrupt
checks inside it, so SIGTERM only lands *between* transactions; confirmed live on 2026-08-29 when
SIGKILL was the only way to stop a SCANNING run) is closed:
- `app/runner.py`: new `TransactionCancelled` exception and `Config.should_stop`, an optional
  zero-arg predicate polled by `Runner._raise_if_cancelled()` at every phase boundary and between
  whole items in the SCANNING / UPLOADING / VERIFYING loops. Deliberately **not** polled inside
  COMMITTING/POST_VERIFY: the manifest/catalog/snapshot-index write is the one part that has to
  land as a unit, and `StateDB.batch()` already made it short.
- A cancellation is **not a failure**: `run_for_date` catches it ahead of its generic handler, leaves
  the phase exactly where it stands, marks nothing FAILED, sends no failure email and pings neither
  healthcheck endpoint (a cancelled run produced no snapshot, so the dead-man *should* go unfed).
- `app/wiring.py`: `build_runner(should_stop=None)`. `app/backupd.py` passes its own
  `_shutdown_event.is_set`.
- A broken `should_stop` that raises is treated as "do not cancel" — a faulty hook must never be able
  to stop a backup that would otherwise succeed.
- `_raise_if_cancelled` reads the field with `getattr(..., None)` because most of this suite builds
  SimpleNamespace config doubles; the real interface is still pinned by a test asserting
  `app.runner.Config` declares `should_stop`, so a rename cannot silently make cancellation a no-op.

**Live rehearsal (not a mock, not production).** A real `backupd` process was run against a scratch
`BACKUP_HOME`, a 400-file scratch source tree and a local-filesystem stand-in for S3 (only
`wiring._build_storage` was substituted; backupd's scheduling, signal handling, wiring, Runner phase
machine, scanner and policy were all the real code). Result:
- SIGTERM delivered while genuinely UPLOADING (220/400 done) → **daemon exited rc=0 in 0.06s**,
  phase preserved at `UPLOADING`, 178 rows still `PENDING_UPLOAD`, nothing marked FAILED.
- Restarted → resumed from `UPLOADING`, carried the 222 already-uploaded rows across without
  redoing them, reached `SUCCESS`, manifest written.
Production `state.sqlite` and the real bucket were not touched at any point.

### 2. `logs/backupd.log` recorded almost nothing about the backup — FIXED
Found while reading the rehearsal's log. `_setup_logging` attached its handlers only to the
`backupd` logger, which is configured `propagate=False`. Every `app.runner` / `app.scanner` /
`app.storage` / `app.policy` record therefore fell through to a **root logger with no handlers**:
INFO was discarded outright and WARNING/ERROR went to logging's lastResort stderr, i.e. into
`supervisor.out`, not the file operators are told to read. The archived evidence is unambiguous — the
entire 17-hour 2026-08-27 canary produced a **194-byte** `backupd.log` holding three scheduler lines.
Fixed by sharing the handlers with the `app` logger namespace (kept `propagate=True` there so pytest's
caplog still works and nothing duplicates, since production attaches nothing to root). The
already-configured check now matches on the resolved filename, not just handler type, so pointing the
daemon at a new log directory can't silently keep writing to the old file. Verified in the rehearsal:
Runner's INFO lines and a full failure traceback now land in `backupd.log`.

### 3. The real `run` path was blind to UNCLASSIFIED — FIXED
Only `backupctl dry-run` produced UNCLASSIFIED accounting; `Runner._scan_source` dropped every
non-selected file with a bare `continue`. That is exactly how `table_read/Media`, `Media_H264` and
`braw_decode` (25 files, ~23 GiB, one of them the sole local copy of a recovery-pending recording)
stayed unprotected through a whole seed without one log line saying so. The scan now counts every
skip by classification, logs the breakdown, and raises a **WARNING naming up to 20 UNCLASSIFIED
paths** — the one outcome that is never intentional. Counting is in-memory and logged rather than
written per-path (the skipped set is routinely hundreds of thousands of files); `dry-run` remains the
exhaustive list. Confirmed live in the rehearsal, which named the planted unadjudicated file exactly.

### 4. `backupctl status` called a dead transaction "in progress" — FIXED
`backup-2026-08-29` (SIGKILLed mid-SCANNING, deliberately preserved) was reported as
*"Current transaction in progress"* for ten days while no backup was running at all. Nothing ever
advances a past day's transaction: Runner only moves one from inside a live process, and backupd only
ever considers *today*. New `_abandoned_transactions()` + `_age_phrase()` in `bin/backupctl`; status
now prints them on their own `ABANDONED transaction:` line with age and the right next action, and
`doctor` has a new **"no abandoned in-progress transaction"** check. This immediately surfaced a
second one nobody had been tracking: **`backup-2026-08-20`, stuck SCANNING for 18 days**, holding 706
`scan_items` rows. The `backupctl run` guard was deliberately **not** extended to these: an abandoned
SCANNING transaction re-scans from scratch on resume anyway, so blocking today's run and telling the
operator to `retry` an old date would be *wrong* advice. (Post-SCANNING abandonment is the opposite
case, and the new status line says so per-phase.)

### 5. `doctor` said PASS while there was no dead-man switch at all — now says so out loud
`BACKUP_HEALTHCHECK_URL` is unset, so every healthcheck ping is a documented no-op — and doctor
printed a clean `PASS healthcheck configuration`. It still passes (the no-op contract is deliberate)
but now prints `WARN: ... NO dead-man monitoring exists. A silent daemon death will not alert anyone.`
There is no env file anywhere in the deployment path: `bin/supervisor.sh` sets no environment at all,
so the URL has to be exported by whatever launches supervisor. **Gate 8 is still not done and needs a
real Healthchecks.io check URL from Daniel.**

### 6. REAL UNPROTECTED DATA under the current policy — needs Daniel's adjudication
Verified with `Policy.classify()` against the live `config/policy.yaml`, on real paths (not probes).
None of these are git repos, so git-rescue does not cover them either, and none have a policy rule at
any depth:

| tree | files | size | last touched |
|---|---|---|---|
| `sparkvsr-pulse-lab` | 1,332 | 3.2 GiB | 09-04 |
| `three_ghosts` | 5 | 298 MiB | 09-07 |
| `_incident_2026-09-04` | 8 | 80 MiB | 09-04 |
| `logs` (top-level) | 7 | 80 MiB | 09-04 |
| `github` | 876 | 53 MiB | older |
| `sparkvsr-prod` | 4 | 22 KiB | 09-02 |
| `melb_acquire/*` (loose root files only) | 363 | 23 MiB | 09-07 |

`melb_acquire` is the sharp one: its subdirectories are covered (`output`, `ABN_bulk`, `data`, …) but
**nothing covers files sitting at its root**, which is where `BATON.md`, `DECISIONS.md` and
`OPUS_PREEXPORT_AUDIT_2026-09-07.md` live. Confirmed `UNCLASSIFIED`/`selected=False` individually.
Not guessed at or auto-added — classification is adjudicated here, never inferred.

### 7. Stale notes corrected
- HANDOFF -3n said the `doctor` S3 fix had "no dedicated test yet". It does:
  `tests/test_storage_bucket_accessible.py` covers `bucket_accessible()` (empty, truncated and
  error cases).
- BATON's BLOCKED entry for top-level `catalog/` is stale: it is `paths.critical` in the live policy
  and classifies `CRITICAL`/selected today.
- `StateDB.source_files` having zero live rows is already documented as rebuild-only in
  `app/restore.py` and asserted in `tests/test_runner_integration.py`. Not a defect to chase.

**Tests: 172 passed, 5 deselected (live), 0 failed** — up from 155 at session start. New files:
`tests/test_transaction_cancellation.py` (10), `tests/test_scan_unclassified_visibility.py` (2), plus
4 abandoned-transaction tests in `test_backupctl_status.py` and 1 daemon-logging test in
`test_backupd_integration.py`. Nine `build_runner` doubles in `test_backupd_integration.py` were
updated to accept the new keyword — a real interface change, not a workaround.

**Not done, still Daniel's call:** gate 7 (re-enable `backupd`/`supervisor`), gate 8 (Healthchecks
URL), gate 9 (controlled restart tests *in production* — the mechanism is now proven, the production
rehearsal is not), gate 10 (next-Melbourne-midnight semantics), the two abandoned transactions
(`2026-08-20`, `2026-08-29`) and their `scan_items` rows, the ~9,302 remote orphan blobs, and the
policy adjudications in §6.

## -3q. Checkpoint 2026-08-27T13:41+10:00 (post-seed gate sequence completed for `backup-2026-08-23`; primary `backupd` blocker fixed plus two more latent bugs found while verifying it; GC dry-run built; production still NOT ready — written before the next live-state change, i.e. before any Aug-27 canary)

**`backup-2026-08-23`: SUCCESS**, 83,772 manifest entries, 70,203 distinct referenced blobs (dedup ratio real, not an error — same-content files share one CAS blob).

**Explicit `backupctl verify`:** 83,772/83,772 remote HeadObject+size PASS, 1,199 sampled download+SHA256 PASS, 0 failures, ~2h34m wall time. This sampling rate (1,199/83,772 content-hashed) is an accepted tradeoff given the exhaustive existence/size pass covers all 83,772 — not re-litigated this session.

**Restore:** the shipped `restore()` could not restore its own manifest at all (0 restored / 83,772 failed against a fully healthy remote, 2026-08-25) because `_manifest_relpath` didn't exist yet — `_normalize_relpath` (built for operator-typed strings) rejected every entry's absolute source path. Fixed (see `app/restore.py`). A real restore drill passed. This session added `tests/test_restore_path_safety.py` (6/6) exercising the real `restore()` — and found two more surprises worth remembering: an absolute manifest path like `/workspace/foo/bar` restores to `<dest>/workspace/foo/bar`, not `<dest>/foo/bar` (only the leading `/` is stripped, no semantic "workspace" anchor is assumed); and `/etc/passwd`-style paths are not literally *rejected*, they're safely *contained* under the destination — the real, intended safety property.

**`rebuild_state`: PASS.** An isolated rebuild (built 2026-08-25 from a scratch DB, still valid since Aug23 is the sole immutable committed snapshot) was compared against production via `ATTACH DATABASE` set-ops: 0 content-level mismatches on manifest identity, all 83,772 snapshot entries, all 70,203 referenced hashes, blob sizes/keys, refcounts. One provenance-only timestamp difference (non-defect: rebuild stamps `committed_at` from the manifest's own `created_at`; production stamps it live). **Real gap found, not a rebuild defect:** production's `StateDB.source_files` table has **zero rows** — `Runner`'s live write path never populates it; the isolated rebuild populates it fully from the manifest. Currently a rebuild-only convenience table with no live coverage. Status of whether this matters: see the source_files investigation below (this same checkpoint).

**COMMITTING (the ~104-minute phase, 03:18–05:02 on the Aug23 run):** root cause is now confirmed, not just suspected — `Runner._upload_manifest` made three individually-committed mutator calls (`record_blob`/`bump_refcount`/`add_snapshot_entry`) per manifest entry against this project's MooseFS-mounted `state.sqlite`, where one durable commit measures ~25–33ms. 83,772 × 3 ≈ 251,000 fsync round-trips = the observed duration. `StateDB.batch()` (a single durable transaction per loop, already wired into both `_upload_manifest` loops) fixes this — 5/5 new tests in `tests/test_statedb_batching.py` prove atomicity, rollback-on-exception, non-batch-path parity, re-entrancy, and byte-identical bookkeeping batched vs. unbatched. **This fix has never been exercised by a real full backup** — it landed after Aug23 already committed. The Aug-27 canary (this session, later) is its first real-world exercise.

**S3, real enumeration:** 79,505 CAS blob objects total; 70,203 referenced by the sole committed snapshot; **~9,302 remote orphan/residue blobs** (mostly from the mid-run WordPress-dump policy correction — those blobs were uploaded, then their `scan_items` rows were deleted directly rather than expired through the normal refcount-decrement path, so they never got local `blobs` bookkeeping rows at all). Zero outstanding multipart uploads. The historical RunPod `QuotaExceeded` (backup-2026-08-19) remains cleared/non-reproducible, unrelated to current work.

**GC:** `GCRunner` had no dry-run mode at all before this session; added `plan()` (returns candidates, mutates nothing — `run()` now just calls `plan()` then deletes). Real read-only dry-run against production: **0 candidates, 0 bytes**, so intersection with the 70,203 Aug23-referenced hashes is trivially 0. **This is not the reassurance it looks like**: production's local `blobs` table contains *exactly* 70,203 rows, all referenced — the ~9,302 real orphan objects were never given local bookkeeping rows, so `GCRunner` (which sources candidates only from local StateDB, never a remote listing) is structurally blind to them. It cannot delete anything wrongly, but it also cannot currently reclaim that real residue at all. **Daniel's call (2026-08-27): this is a reclamation-completeness gap, not a backup-safety blocker** — GC's failure mode here is wasted volume capacity, not risk to committed data, so it does not gate enabling `backupd`. Remote-orphan reconciliation (remote listing − all-retained-snapshot-referenced − in-flight-transaction-protected − grace-period) is a separate, unbuilt future capability; do not build it reactively inside an existing automatic GC path.

**`backupd` — the primary blocker, now fixed, plus two more found while verifying it:**
1. Historical implementation was a permanent nightly no-op: `_run_transaction` checked `hasattr(app_runner, "run_today")`, which was always False (that function never existed), so every cycle since 2026-08-17 recorded `PHASE_NOT_CONFIGURED` — seven Aug17–23 scheduled executions, all no-ops. Fixed: `_run_transaction` now calls the real `app.wiring.build_runner().run_for_date(...)`. New module `app/wiring.py` extracts the Runner-construction wiring that used to live only in `bin/backupctl`'s private `_build_runner`, so both the CLI and the daemon build an identically-wired Runner from one source of truth (this project's own convention — duplicate wiring has caused interface-mismatch bugs here before). `PHASE_NOT_CONFIGURED` is retired entirely.
2. **Found while verifying #1, not by the original directive:** `backupd`'s phase-polling read `app.statedb.StateDB`'s `runs` table — a table `Runner` has *never* written to (Runner's real phase lives exclusively in its own `transactions` table, written via its own raw sqlite3 connection). Before this fix that was merely dead bookkeeping (backupd wrote its own placeholder rows into `runs`); removing backupd's writes to `runs` (correct, per single-writer principle — Runner is now the sole writer to real backup state) would otherwise have made backupd poll a permanently-empty table forever, busy-looping with zero sleep and rebuilding a full Runner (S3 client and all) every iteration. Fixed with a direct read-only peek at Runner's real `transactions` table (`BackupDaemon._get_transaction_phase`).
3. **Also found while verifying #1:** `_seconds_until_next_local_midnight` subtracted two aware `datetime` objects sharing the identical `ZoneInfo` instance — confirmed empirically against real `zoneinfo` behavior that CPython's `datetime.__sub__` takes a naive-subtraction shortcut in that case, silently wrong by exactly the 1-hour DST shift on Melbourne's two transition nights/year (2026-10-04 spring-forward, 2026-04-05 fall-back). Fixed via `.timestamp()` differencing on both sides.
17/17 new tests in `tests/test_backupd_integration.py` pass, covering all 10 required daemon/Runner-boundary cases plus DST-specific numeric assertions. **`backupd` has never been live-exercised with this new wiring** — everything above is proven against a fake Runner double; the Aug-27 canary (this session, later, if gates 1–5 below are clean) is its first live test.

**Policy:** `website/DL_Dump.zip` (455MB, confirmed via `unzip -l` to be a reproducible copy of the same already-adjudicated WordPress dump) was a real, previously-unpatched leak — the existing `website/DL_Dump` rule only matched the directory, not the sibling `.zip` file, because policy path-matching is component-exact. Fixed in `config/policy.yaml` + regression test; 30/30 policy tests pass. **Current `config/policy.yaml` now legitimately differs from the policy that produced the Aug23 scan** (this fix, plus whatever the Aug23 mid-run correction already changed) — any future snapshot's `policy_hash` will differ from Aug23's, which is expected, not a defect.

**Full non-live suite: 99 passed, 5 deselected (live), 0 failed.**

**PRODUCTION READY: NO — pending a controlled live `backupd` canary.** Daniel's revised call on blocker priority (2026-08-27, supersedes this session's initial framing): the live canary is the *only* real production blocker. GC's remote-orphan blind spot is a reclamation-completeness gap (wasted capacity, not risk to committed data) and does not gate enabling `backupd`. `source_files` being rebuild-only is not a blocker unless a live feature is found to depend on it (being checked, this same session, before the canary). Exact policy-byte provenance (not just `policy_hash`) is being fixed *before* the next real transaction, precisely because Aug23's mid-run policy edit already proved a hash alone can outlive the bytes that produced it.

**Do not re-enable `supervisor`/unattended `backupd` yet. Do not run destructive GC.** A single controlled, observed, manually-stopped `backupd` canary for 2026-08-27 is authorized (Daniel, 2026-08-27) once policy provenance is fixed and the 5 live tests pass — see the continuation of this session for the outcome.

## -3p. Checkpoint 2026-08-24 (`backup-2026-08-23` reached SUCCESS — full success-gate report; do NOT re-enable backupd/supervisor or start the post-seed verify/restore-drill gates without Daniel's explicit go-ahead)

**`phase=SUCCESS`, `manifest_committed=1`, `catalog_uploaded=1`, `retention_done=1`, `gc_done=0`
(expected — GC only runs on the configured `gc_weekday`, not every run), `error=None`.** COMMITTING
took unusually long (~104 min, 03:18–05:02) — confirmed genuinely active the whole time (state.sqlite
grew 217M→236M+, CPU time climbing throughout, no lock/disk-space issues found), not a stall;
root cause not further instrumented (likely the ~83,772×3 individual local `record_blob`/
`bump_refcount`/`add_snapshot_entry` writes over this project's known-slow network-mounted
filesystem) — flagged to Daniel live, worth a closer look later if it recurs on a normal nightly run
rather than a one-off manual seed.

**TRANSACTION:** `backup-2026-08-23`, `policy_hash=2524874aaa2aa851f54d68b746a4fc7f5cbbbc492fe250a059b349be259cce3b`
(unchanged since pre-launch), final phase `SUCCESS`, `resume_phase=NULL`.

**SCAN (final, post mid-run correction):** 83,782 files / 180,285,512,703 bytes (~167.92 GiB)
selected. Classification breakdown: OUTPUTS 51,966/~95.08 GiB, CRITICAL 21,825/~36.31 GiB, BTS
959/~30.44 GiB, GIT_RESCUE 9,028/~6.04 GiB, SQLITE_DATABASES 4/~30 MiB.

**UPLOAD:** 83,772 files `UPLOADED` (180,285,238,171 bytes, ~167.92 GiB), 10 `DEFERRED_ACTIVE`
(274,532 bytes — concurrent live `comfy`/`erf` activity throughout the run, correctly excluded from
the manifest). `deferred_items` total 991 rows across the transaction's whole lifetime (981 from the
original pass + 10 more after the mid-run correction/resume) — an audit trail, not current state.
**Zero rows with a non-NULL `error`** — no upload failures, no retries visibly needed beyond the
routine deferred-active handling.

**MANIFEST — verified against the real committed object, not just the local DB**
(`_osf_backup/manifests/backup-2026-08-23.json`, fetched directly via `get_object_bytes`):
83,772 entries, 991 deferred, total bytes `180,285,238,171` — exact match to `scan_items`. **Zero**
entries under any of the four excluded `website/` prefixes (`DL_Dump`, `wetransfer_...`, `recovery`,
`recovery-backups`) — the mid-run exclusion is real and confirmed in the actual artifact clients will
restore from, not just in local bookkeeping. `table_read/Media/3018_10171415_C001.braw.PARTIAL`
present, `CRITICAL`, `sha256=0493fb5850bcf415ffdac0e0f3dc842910eb8a7bdcd32652d58d011baaee5b99`,
`size=19260243968` — exact match to the live file.

**POLICY SPOT-CHECKS (final `scan_items`, all as required):**
- Protected: `table_read/Media/...C001.braw.PARTIAL` (1, CRITICAL, UPLOADED), `table_read/Media_H264`
  (4), `table_read/braw_decode` (20), `erf/media/music_risk_review` (10).
- Excluded (all 0 rows): `erf/media/{keyframes,proxies,thumbs}`, `platelock/env`,
  `POC_VFX/GeneralAssets/houdini_caches`, and all 4 excluded `website/` prefixes.

**S3 bucket final state** (real `list_objects_v2` enumeration under `_osf_backup/`, full pagination):
**79,508 objects, 176,288,360,169 bytes (~164.18 GiB), zero outstanding multipart uploads.** Object
count is lower than the manifest's 83,772 entries because of real CAS deduplication (identical files
share one blob) plus the count including non-blob objects (manifests/, catalog/, meta/) — expected
and healthy, not a discrepancy. Zero outstanding multipart uploads confirms every multipart transfer
(including the 19.26 GiB C001 file) completed cleanly with no orphaned state.

**Still true, unconditionally:** `backupd`/`supervisor` stay OFF. Daniel's full post-seed gate
sequence (explicit `backupctl verify`, a real restore drill, isolated `rebuild_state`, comparison
with production logical state, dealing cleanly with `backup-2026-08-19`'s historical FAILED status,
only then re-enabling the daemon, configuring Healthchecks, controlled restart tests, confirming next
Melbourne-midnight semantics) has **not started** — SUCCESS here is the trigger to ask Daniel for
that go-ahead, not to proceed automatically.

## -3o. Checkpoint 2026-08-24 (mid-run correction: excluded 4 redundant WordPress-dump subtrees from `backup-2026-08-23`, repeated the -3i date-rollover mistake once while doing it, caught and fixed it — now VERIFYING)

**Mid-run policy correction, Daniel's call:** with `backup-2026-08-23` ~99% through UPLOADING,
Daniel asked what the remaining ~48K pending files were. All of them were inside `website/` — three
near-duplicate WordPress backups of the same site (`website/DL_Dump`, a WeTransfer export
`website/wetransfer_u797478838_xubk0-3-sql_2025-11-25_2206`, `website/recovery`) plus
`website/recovery-backups`, ~44,000 tiny PHP/JS/CSS files (~1.66 GiB) dominating the tail on
per-file round-trip latency, not real content. Daniel: exclude them. Added all four as
`paths.redownloadable` in `policy.yaml` (longest-prefix-wins over the broad `outputs: - website`
rule; the rest of `website/` — `index.html`, `Docs/`, the `.sql` dump, `code-handoff-inventory` —
stays `OUTPUTS`/selected). Live-verified, 2 new regression tests, full policy suite **29/29 passed**.

**This does not retroactively affect an already-scanned transaction** (same as every other policy
fix this session) — `backup-2026-08-23`'s `scan_items` were already fixed from SCANNING. Daniel then
said: "Stop the current transfer and call it complete." Executed as: kill the running process
(clean SIGTERM, no error recorded, `phase` untouched) → delete all `scan_items` rows matching the 4
excluded prefixes for `backup-2026-08-23` specifically (56,668 rows removed, including 9,549 files/
457 MB of `website/DL_Dump` that had already uploaded — those blobs stay in CAS as harmless
unreferenced objects, not deleted, same GC-later pattern as every other exclusion this session) →
resume via real application code (not a hand-fabricated SUCCESS) so `_upload`/`_verify`/`_commit`/
`_post_verify` all run for real against the corrected `scan_items`.

**Repeated the exact -3i date-rollover mistake once, caught fast:** relaunched with
`backupctl run --force-new-date` — the pod's system clock is UTC and Melbourne is UTC+10, so the
relaunch at pod-time 23:40 UTC landed at Melbourne 09:40 on **2026-08-24**, past midnight. `run`
always targets `_melbourne_today()`; `--force-new-date` just bypasses the older-FAILED-transaction
guard, so it happily started a **brand new `backup-2026-08-24` transaction and a fresh rescan**
instead of resuming `backup-2026-08-23`. Caught within ~15-20s (706 scan_items rows written, nowhere
near real progress), killed cleanly, and both the `backup-2026-08-24` transaction row and its 706
`scan_items` rows were deleted (harmless, never resumed, not worth keeping as clutter).
**Correct fix: `backupctl retry <date>` calls `run_for_date(args.date)` directly with the exact date
given — no dependency on "today," no FAILED-phase requirement at the CLI level** (`run_for_date`
itself falls through cleanly for any non-SUCCESS phase, including UPLOADING, straight into
`_run_phases`, which re-enters at whatever phase `_get_phase` reports). `backupctl retry 2026-08-23`
resumed correctly: `_upload` finished the remaining ~39 tiny non-excluded files, advanced to
VERIFYING for real.

**Lesson, now doubly-confirmed (this is the second time this exact trap has bitten a session):** never
use `backupctl run`/`--force-new-date` to resume a specific known transaction — always use
`backupctl retry <exact-date>`. `run` is only correct for "start today's transaction," and "today" is
a moving target across a Melbourne midnight that this pod's UTC system clock makes easy to misjudge
mid-session.

**State as of this entry:** `backup-2026-08-23` — `phase=VERIFYING`, real HeadObject + sampled
download-verify in progress (411/83,782 done at last check), no error. UPLOADING finished clean:
83,772 files / ~167.9 GiB `UPLOADED`, 10 `DEFERRED_ACTIVE` (unchanged all session, concurrent live
`comfy`/`erf` activity, correctly excluded from the manifest). `backup-2026-08-19` and the older
`backup-2026-08-20` stray (a pre-existing historical artifact, not from this session) are untouched.

## -3n. Checkpoint 2026-08-23 (Daniel gave explicit GO — fresh policy-directed seed `backup-2026-08-23` launched, IN PROGRESS — do not touch until it reaches SUCCESS or FAILS)

**GO given by Daniel** for a completely fresh seed (not a retry of `backup-2026-08-19`, which stays
`FAILED` as historical evidence of an incomplete old-policy scan + the now-cleared transient S3
incident — not touched, not mutated, not fabricated to SUCCESS).

**Preflight, all done for real immediately before launch:**
- Full non-live suite still green (no code changes since the last full run except the doctor fix
  below, which has no test coverage of its own yet — see note).
- `config/policy.yaml` confirmed to contain all corrections from checkpoint -3m
  (erf/media/{keyframes,proxies,thumbs}, platelock/env, houdini_caches -> excluded;
  table_read/{braw_decode,Media,Media_H264} -> protected).
- **`policy_hash` recorded**: `2524874aaa2aa851f54d68b746a4fc7f5cbbbc492fe250a059b349be259cce3b`.
  Raw `sha256sum config/policy.yaml`:
  `aee461bd039a7aa930097ab2013a7b598c807e5dc3b27c91fdac4b7572da0bd0`.
- **No git repo in `_BACKUP`** (confirmed: `git status` -> "not a git repository") — there is no
  commit hash to record. Code state for this launch is exactly what checkpoints -3l/-3m/-3n describe;
  no code changed between finishing -3m and this launch except the doctor fix immediately below.
- **Found and fixed a real bug blocking the doctor preflight gate itself**: `bin/backupctl`'s
  `doctor` S3 check called `storage.list_prefix("", max_keys=1)` — `list_prefix` intentionally raises
  `ListPrefixLimitExceeded` if the real result is truncated (it's a bounded-single-page helper, used
  correctly elsewhere for narrow prefixes). Against the whole bucket with `max_keys=1`, this has
  raised on every call since the bucket held more than 1 object anywhere (since 2026-08-19's first
  blob), permanently misreporting "S3 authentication or bucket visibility failed" even when auth and
  visibility were both fine. Verified via a direct raw `list_objects_v2` call and via calling
  `storage.list_prefix` directly (same traceback, `ListPrefixLimitExceeded`, not a `ClientError`) —
  not a credential problem (clock also checked: system time matched an external reference exactly, no
  skew). Fix: catch `ListPrefixLimitExceeded` in the doctor check specifically and treat it as PASS —
  reaching "IsTruncated" only happens after `list_objects_v2` already succeeded with real auth against
  a real, real bucket. Verified live: doctor's S3 check now PASSes. **No dedicated regression test
  written for this one** — judgment call given the time-sensitivity of the launch; it's a small,
  already-live-verified CLI-reporting fix orthogonal to the classification/data-loss-risk logic this
  session's other work actually tested. Flagged, not silently skipped.
- **Stopped `bin/supervisor.sh` (pid 31899) and its child `backupd.py` (pid 36237)**, both running
  since 2026-08-16/17 — the opposite of Daniel's "keep backupd/supervisor DISABLED during this manual
  seed" instruction. Sent SIGTERM to the supervisor (not the daemon directly — the supervisor
  restarts its child ~10s after any exit, confirmed via `bin/supervisor.sh`'s own loop); both exited
  cleanly per `logs/supervisor.log`. Confirmed neither is running before launch.
- **S3 diagnostic-prefix check**: all 7 named `_diag_*` prefixes from the quota investigation
  (`_diag_s3_quota_20260822T232142Z/`, etc.) are confirmed **empty** (0 objects, 0 bytes each, real
  `list_objects_v2` query, not just a `CommonPrefixes` listing) — genuinely cleaned up already.
  Also found ~35 orphaned `_test_runner_*`/`_test_s3transfer_*`/`_test_s3diag_*` prefixes from this
  project's own live test suite (`tests/test_runner_integration.py`'s `env` fixture generates a fresh
  UUID key prefix per run and cleans up at teardown) — also confirmed **empty**, no real cleanup
  needed. **Found, not cleaned up (non-blocking, flagged for later)**: ~363 MB of real objects under
  `.cache/`, `.claude/`, `.pod-persist/`, `install_claude.log`, `install_claude.sh`, `untitled.txt`,
  `.ipynb_checkpoints/` — almost certainly bootstrap files from the second Code instance (-3k
  addendum) that had this same volume mounted directly as its own `/workspace`. Outside
  `_osf_backup/`, can't collide with or be included in this backup's manifest, 0.13% of the 279.4 GiB
  capacity — immaterial to this launch, left alone rather than deleting bucket content unilaterally
  without Daniel's explicit go-ahead.
- **No outstanding multipart uploads** (`list_multipart_uploads`, real call, 0 results).
- **No other Runner process active**: confirmed via `flock`-based liveness check on
  `state/backupd.lock` (free) and `fuser state/state.sqlite` (found and killed two of *my own*
  leftover background reconciliation scripts from the read-only Phase 3/4 analysis earlier this
  session — real processes, not zombies, just far slower than expected over the network mount;
  neither had written anything, both were plain `SELECT`-only; killed cleanly before launch so they
  wouldn't contend with the seed's own I/O on the same mount).

**Launch**: `BACKUP_HOME=/workspace/_BACKUP .venv/bin/python bin/backupctl run --force-new-date`, via
this session's own tracked background-execution (not raw `nohup` — that was the -3h mistake, caught
and avoided again here), output to `logs/seed_20260823.out`.

**Confirmed within ~25s of launch** (not assumed): `transactions` row for `backup-2026-08-23` exists,
`phase=SCANNING`, `resume_phase=NULL`, `policy_hash=2524874aaa2aa851f54d68b746a4fc7f5cbbbc492fe250a059b349be259cce3b`
— **matches the recorded pre-launch hash exactly**, live proof the policy-fingerprint guard (checkpoint
-3m PHASE 6) is wired correctly end to end, not just unit-tested. Real PID confirmed running (not
orphaned). `backup-2026-08-19` untouched (still `FAILED`/`resume_phase=UPLOADING`, separate txn_id).

**Expected duration**: the 2026-08-17 dry-run measured the full source tree at 752,659 files / 1,666
GiB; that SCANNING phase alone took ~5.4h previously. This corrected-policy scan walks the same tree
(classification changes what's *selected*, not what's *walked*), so a multi-hour SCANNING phase is
expected, not a hang. **On resume: check the background-task completion notification first; if none
has arrived, check `ps aux | grep "backupctl run"` and the `transactions` row's
`phase`/`updated_at` directly rather than assuming success or a hang** (same guidance as -3h).

**Not yet enabled**: `backupd`/`supervisor` stay off until `backup-2026-08-23` reaches genuine
`SUCCESS` and Daniel's post-seed gate sequence (verify / restore drill / isolated `rebuild_state` /
controlled restart tests) completes — see BATON NOW for the full remaining gate list.

**SCANNING completed 2026-08-24T01:25:39+10:00** (~9.7h elapsed — slower early pace than the ~5.4h
historical precedent while walking dense `comfy`/`pipeline`/`motiongen_tests` venvs, consistent with
per-file overhead not total-tree size). End-of-scan checkpoint, all sane, Daniel's explicit "do not
stop for approval if sane" standing order applied — did NOT pause, let it continue straight to
UPLOADING:

- **140,450 files / 182,421,349,860 bytes (~169.92 GiB)** selected. Breakdown: OUTPUTS 108,634/104.22
  GiB, CRITICAL 21,825/38.99 GiB, BTS 959/32.69 GiB, GIT_RESCUE 9,028/6.49 GiB, SQLITE_DATABASES
  4/30 MiB. Well under Daniel's 210 GiB investigate-first threshold.
- **Presence, exact byte matches to live-disk measurements taken earlier this session**:
  `table_read/Media/3018_10171415_C001.braw.PARTIAL` present, 19,260,243,968 B, CRITICAL;
  `table_read/Media_H264` 4 files/5,270,891,950 B; `table_read/braw_decode` 20 files/125,653,338 B;
  `erf/media/music_risk_review` 10 files/92,452,831 B.
- **Absence, all zero**: `erf/media/{keyframes,proxies,thumbs}`, `platelock/env`,
  `POC_VFX/GeneralAssets/houdini_caches`, `footage/DAY_{1,2,3}/RUSHES` (principal),
  `_BACKUP/state` (hard_exclude — the scanner's own blob_cache walk at the tail of SCANNING stayed
  correctly excluded despite being walked).
- `policy_hash` unchanged/non-NULL, matches the pre-launch recorded hash exactly, still.
- 981 `deferred_items` rows, all `DEFERRED_ACTIVE` (files touched within the 600s settling window
  during a ~9.7h scan — expected, not an error).
- **Not a reuse of Aug-19's scan**: `backup-2026-08-19` occupies `scan_items` rowids 1–155,853;
  `backup-2026-08-23` occupies 156,560–297,009 — disjoint ranges, proving a genuinely fresh walk.
- **Gap found, not blocking**: the real `run` path logs zero UNCLASSIFIED activity (only
  `dry-run`/`inventory` do) — `_scan_source` silently `continue`s past unselected files with no
  count/log line, so "count/bytes of UNCLASSIFIED for this run" isn't answerable from this
  transaction's own artifacts without a separate multi-hour `dry-run` pass, which wasn't warranted
  mid-upload. Real gap against this project's own "explicit outcomes only" principle — flagged for a
  future session, not chased now.

**UPLOADING as of this entry**: 1,713 files / 33.49 GiB already `UPLOADED` within roughly a minute of
entering the phase — consistent with deduplication against the 89.57 GiB of blobs already sitting in
CAS from the `backup-2026-08-19` attempt (not touched, not deleted, exactly the "existing CAS blobs
should deduplicate normally" behavior Daniel called for). 148,935,226,918 B (~138.7 GiB) still
`PENDING_UPLOAD`. Monitoring continues; still no approval pause per standing order unless a real
failure or anomaly appears.

## -3m. Checkpoint 2026-08-23 continued (S3 quota reclassified historical/cleared; five more policy corrections landed; policy-fingerprint resume guard built and tested; still no retry — RESCAN now the live question, pending table_read findings)

**S3 STATUS — RunPod `QuotaExceeded` reclassified: HISTORICAL / TRANSIENT / CURRENTLY CLEARED, not an
active blocker.** Daniel proved, against the same production bucket/implementation: ordinary S3 writes
now cross the old 100 GB boundary; the exact 107 KiB file that historically triggered `QuotaExceeded`
now uploads cleanly; a production 600 MiB multipart upload succeeds; the 128 MiB multipart threshold
sits safely below the observed plain-PUT gateway ceiling; no current `QuotaExceeded` is reproducible by
file, size, single PUT, multipart, or total stored bytes. **Evidence and incident history in -3k below
are retained as-is** (useful RunPod-provider-behavior documentation, not deleted) — only the
"is this currently blocking us" verdict changes.

**PHASE 1 — five more policy corrections, live-verified against `Policy.classify()`, all in
`config/policy.yaml`:**

| path | before | after | why |
|---|---|---|---|
| `platelock/env` | OUTPUTS (selected), 8.6 GiB | REDOWNLOADABLE (excluded) | Confirmed conventional `python3.12 -m venv` (`pyvenv.cfg` present, only standard `bin/lib/lib64/include/share`, no other files) — not caught by any policy rule or by `scanner.py`'s `DEFAULT_REDOWNLOADABLE_DIR_NAMES` (`venv`/`venvs`/`.venv` listed, bare `env` was not). |
| `POC_VFX/GeneralAssets/houdini_caches` | CRITICAL (selected) — the intended `redownloadable` rule was **dead**, shadowed by an identical-prefix `critical` rule parsed first (matcher tie-break: first-rule-wins on equal prefix length) | REDOWNLOADABLE (excluded) | Audited the actual tree: 2 files total, both `Flame*.zip` VDB cache archives, **no** `.hip`/`.hipnc` scenes present anywhere under it. The `critical` entry's premise ("might have authored scenes in here too") doesn't hold for the real current contents, so it was removed rather than reordered — makes the rule set unambiguous instead of relying on parse-order luck. |
| `table_read/braw_decode` | UNCLASSIFIED (not backed up) | CRITICAL (selected) | 124 MiB, mixed authored `.cpp`/`.sh` + QA comparison stills (`qa_stills/*.png`, braw vs h264 vs h265) + rebuildable `.o`/binary + logs. A prior "keep the whole directory" decision existed only in `BATON.md`, never encoded — same unencoded-ruling pattern as `erf/media`. |
| `table_read/Media` | UNCLASSIFIED (not backed up) | CRITICAL (selected) | Contains exactly one file: `3018_10171415_C001.braw.PARTIAL`, 19,260,243,968 bytes (~17.94 GiB), the recovery-pending BRAW recording named in the original task. No complete `.braw` replacement found anywhere on `/workspace`; no H.264 proxy exists for C001 either. **This is the only local copy of C001, in any form, and it was not being backed up at all.** |
| `table_read/Media_H264` | UNCLASSIFIED (not backed up) | CRITICAL (selected) | 4 files (C002–C005), 5.0 GiB. No raw `.braw` counterpart found on `/workspace` for any of the four — these H.264 files appear to be the only local copies of that footage, not disposable proxies of a retained original. |

`table_read/Roles` (already CRITICAL) and `table_read/_braw_toolchain` (already REDOWNLOADABLE, "not
authored, Blackmagic SDK") were already correct and untouched.

**PHASE 2 — tests:** added 6 more `Policy.from_yaml`-backed regression tests (the 5 corrections above,
plus a sibling-path test proving the `houdini_caches` fix didn't broaden the exclusion to the rest of
`POC_VFX/GeneralAssets`). `tests/test_policy_regression.py`: **27/27 passed.**

**PHASE 6 — policy-fingerprint resume guard, built (not deferred — this is unconditional regardless of
the rescan decision):** `app/runner.py` — `transactions` gets a new `policy_hash TEXT` column (same
`ALTER TABLE ... IF NOT EXISTS`-column migration pattern already used for `resume_phase`). `_scan()`
now stamps `transactions.policy_hash = config.policy.policy_hash` every time it (re)populates
`scan_items`. `run_for_date`'s FAILED-resume branch now calls a new `_check_resumable_policy(txn_id)`
before `_resume_transaction(...)`: if the transaction's stored `policy_hash` doesn't match the current
`config.policy.policy_hash` — **including `NULL`, treated as "unknown policy" on pre-migration
transactions** — it raises `RuntimeError` naming both hashes and pointing at reconciling/rescanning,
and resume never happens. Fails closed, not open; no silent reclassification, no silent stale-continue.
`tests/test_runner_failed_resume.py`: added `test_run_for_date_refuses_resume_when_policy_changed`,
`test_run_for_date_refuses_resume_when_policy_hash_is_null`, and
`test_run_for_date_resumes_after_policy_hash_reconciled` (stands in for "changed policy + successful
reconciliation -> retry allowed" since no reconcile CLI exists yet — see PHASE 5 note below); updated
the pre-existing same-policy resume test to seed a matching `policy_hash` first (it did not previously
set one, and now needs to since `_make_runner`'s stub `config.policy` didn't exist before this).
**6/6 passed.** This closes the exact silent-failure path found earlier this session: from now on, any
`policy.yaml` edit between a scan and a resumed retry hard-fails instead of silently uploading (or
silently skipping) the wrong file set. Because `backup-2026-08-19` predates this column, its
`policy_hash` is `NULL` after the next `_init_schema()` migration runs — meaning **this guard alone,
with zero other action, already blocks a naive `backupctl retry 2026-08-19`** once this code ships;
did not need to hand-construct that protection separately.

**PHASE 5 — reconciliation CLI (`backupctl reconcile <date> --policy-current --dry-run/--apply`):
deliberately NOT built this pass.** Reasoning, not an oversight: Daniel's own Phase 4 rule is "if any
currently-required file is absent from the preserved scan, RESCAN REQUIRED = YES, don't synthesize a
partially-updated historical scan." `table_read/Media` and `table_read/Media_H264` — both newly CRITICAL
above — have **zero rows** in `backup-2026-08-19`'s `scan_items` (confirmed by direct query, see PHASE 3
below), so they were never scanned/hashed at all. That makes RESCAN REQUIRED = YES for this
transaction regardless of what a reconciliation tool could do, which means there is no real scenario
to build/validate `--apply` against right now — writing an unexercised state-mutating CLI path against
that possibility would be exactly the "half-finished implementation for a hypothetical" this project's
own conventions warn against. The **dry-run analysis itself** (Phase 3, below) was still done, read-only,
against the real `Policy`/`scan_items` — just via direct queries rather than new CLI plumbing, since
building the CLI wasn't needed to get that answer honestly. If a future incident is genuinely
reconcile-eligible (every change is `selected->excluded` or `selected-A->selected-B`, nothing newly
required is missing from the scan), building Phase 5 for real, against that real case, is the right time
to do it.

**PHASE 3/4 — Aug-19 scan_items reconciliation and rescan decision: DONE.** Method note: a full
per-row `Policy.classify()` pass (155,853 rows, including real filesystem marker checks) was
attempted twice over the network-mounted `/workspace` and both times the background process died
silently partway through (no error, no traceback, no OOM message -- just stopped producing output;
not investigated further since it wasn't needed, see below). Used a **diff-based proof** instead,
which is exact, not an approximation: `_match_path_rule` is purely prefix-based with no global state
(`app/policy.py`), so a `scan_items` row's classification can only change between old and new policy
if its path falls under one of the prefixes this session's diff actually touched. This session's
`policy.yaml` diff touched exactly four prefixes: `erf/media/{keyframes,proxies,thumbs}` (added to
`redownloadable`), `platelock/env` (added to `redownloadable`), `POC_VFX/GeneralAssets/houdini_caches`
(removed from `critical`), and `table_read/{braw_decode,Media,Media_H264}` (added to `critical`).
Every other row in `scan_items` is provably unaffected. Counts for all seven touched prefixes were
independently queried against the real `state.sqlite` (SQL) and the real `Policy.classify()`
(spot-checks) earlier in this session -- both agree.

**Reconciliation, `backup-2026-08-19` (155,853 rows / 163,254,042,446 bytes, ~152.03 GiB total):**

1. **SELECTED -> SELECTED** (untouched by this session's diff): 133,917 rows / 150,502,839,441 bytes
   (~140.18 GiB).
2. **SELECTED -> EXCLUDED**: **21,936 rows / 12,751,203,005 bytes (~11.88 GiB)**.
   - `erf/media/{keyframes,proxies,thumbs}`: 2,513 rows / 4,460,185,283 B (~4.15 GiB)
   - `platelock/env`: 19,421 rows / 7,217,295,653 B (~6.72 GiB)
   - `POC_VFX/GeneralAssets/houdini_caches`: 2 rows / 1,073,722,069 B (~1.00 GiB)
3. **SELECTED class A -> SELECTED class B**: **0 rows** — none of this session's corrections move a
   file between two still-selected classes; every change is either a narrowing (bucket 2) or a
   previously-unselected path becoming newly selected (bucket 4).
4. **NOT REPRESENTED / previously unselected -> NOW SELECTED**: **25 files / 24,656,789,256 bytes
   (~22.96 GiB)**, confirmed absent from `scan_items` by direct query (0 rows each, before this
   session's fix):
   - `table_read/Media` (`3018_10171415_C001.braw.PARTIAL`): 1 file / 19,260,243,968 B (~17.94 GiB)
   - `table_read/Media_H264`: 4 files / 5,270,891,950 B (~4.91 GiB)
   - `table_read/braw_decode`: 20 files / 125,653,338 B (~119.8 MiB)

**RESCAN REQUIRED: YES.**
**REASON:** Bucket 4 is non-empty. 25 real, now-CRITICAL files (~22.96 GiB) — most significantly the
19.26 GiB `C001.braw.PARTIAL`, the sole local copy of that recovery-pending recording — were never
enumerated or hashed by the Aug-19 scan, because the old policy left `table_read/Media`,
`table_read/Media_H264`, and `table_read/braw_decode` UNCLASSIFIED. Per Daniel's own Phase 4 rule,
this alone is sufficient: do not synthesize/inject rows into the preserved scan to avoid a rescan: "a
complete new policy-directed scan is safer than synthesizing a partially updated historical scan."
`erf/media`'s independent growth since the Aug-19 scan (§-3l) is irrelevant to this verdict either
way — bucket 4 alone decides it, and buckets 2/3 alone (had bucket 4 been empty) would NOT have
required a rescan.

**PHASE 5 confirmed correctly deferred:** since RESCAN REQUIRED = YES, there is no valid target for a
`backupctl reconcile ... --apply` against `backup-2026-08-19` — it will be superseded by the fresh
scan. Not built this pass (see PHASE 5 note above); the dry-run-equivalent analysis above was produced
read-only, without needing that CLI, which is what actually mattered this session.

**Net state:** S3 is no longer a blocker (see S3 STATUS above). Policy is believed complete for every
area audited. The policy-fingerprint resume guard (PHASE 6) means `backupctl retry 2026-08-19` would
now hard-refuse on its own (its `policy_hash` is `NULL` post-migration) rather than silently resuming
under stale classification even if someone tried it. **The correct next action is a fresh
policy-directed scan, not a retry of `backup-2026-08-19`.** Still do not run `backupctl retry
2026-08-19` or `backupctl run` — that decision (when/how to launch the fresh scan) is Daniel's/Opus's
call, not taken here.

## -3l. Checkpoint 2026-08-23 (erf/media policy-conformance gate: the adjudicated keyframes/proxies/thumbs REDOWNLOADABLE ruling was never encoded in policy.yaml — fixed, tested, and reconciled against the existing scan; two more unencoded/dead rulings found nearby; retry still blocked — new blocker found, not just -3k's RunPod one)

**Trigger:** re-examined the `backup-2026-08-19` blocker from a different angle and found that the
`erf/media/{keyframes,proxies,thumbs}` = REGENERABLE/exclude, `erf/media/music_risk_review` = KEEP
adjudication from the earlier classification audit had never actually been written into
`config/policy.yaml` — it only ever existed in conversation. `policy.yaml` still had the single broad
`outputs: - erf/media` rule (line 272 pre-fix), so the whole 4.9 GiB tree, including the ~4.68 GiB of
pipeline-regenerable derived media, was OUTPUTS/selected. This is a **separate, additional** defect
from the -3k RunPod S3 gateway quota issue — both must clear before a retry is safe/correct.

**TASK 1 — erf/media inventory (confirmed via `du`/`ls`, no hidden siblings):**
| path | size | pre-fix class | post-fix class |
|---|---|---|---|
| `erf/media/proxies` | 3.9 GiB | OUTPUTS (selected) | REDOWNLOADABLE (excluded) |
| `erf/media/keyframes` | 685 MiB | OUTPUTS (selected) | REDOWNLOADABLE (excluded) |
| `erf/media/thumbs` | 309 MiB | OUTPUTS (selected) | REDOWNLOADABLE (excluded) |
| `erf/media/music_risk_review` | 91 MiB | OUTPUTS (selected) | OUTPUTS (selected, unchanged) |

**TASK 2 — policy.yaml fix:** added `erf/media/keyframes`, `erf/media/proxies`, `erf/media/thumbs` as
three new entries under the existing `paths.redownloadable:` list (no new vocabulary — `REDOWNLOADABLE`
already exists and is already "payload excluded, not selected" per `app/policy.py` `classify()`).
Relies on the matcher's real longest-prefix-wins behaviour (`_match_path_rule`, `len(rule.prefix) >
len(best.prefix)`, strict `>`): the 3-segment new rules win over the 2-segment `outputs: - erf/media`
rule for files under those exact subtrees; `music_risk_review` and anything else directly under
`erf/media` still fall through to the unchanged `outputs: - erf/media` rule. No `**` or partial-segment
globs used — `_validate_rule_parts` rejects those at load time, confirmed by the existing
`test_recursive_glob_component_raises_at_load_time...` test.

**TASK 3 — regression tests:** added `test_erf_media_derived_subtrees_are_redownloadable` and
`test_erf_media_music_risk_review_stays_protected` to `tests/test_policy_regression.py`, exercising the
real `Policy.from_yaml(config/policy.yaml)` loader/matcher (not a synthetic fixture), same pattern as
the existing plate-rule and catalog/.claude tests. Full suite: **21/21 passed**
(`BACKUP_HOME=/workspace/_BACKUP .venv/bin/python -m pytest tests/test_policy_regression.py -q`).

**TASK 5 — reconciliation against the existing `backup-2026-08-19` scan** (state.sqlite `scan_items`,
read-only queries, no mutation):
- Total scan_items for `backup-2026-08-19`: 155,853 rows / 163,254,042,446 bytes (~152.0 GiB).
- OLD SELECTED -> NOW EXCLUDED (keyframes+proxies+thumbs): **2,513 rows / 4,460,185,283 bytes (~4.15
  GiB)**. Breakdown: keyframes 946 rows/331,658,162 B; proxies 311 rows/4,118,383,673 B; thumbs 1,256
  rows/10,143,448 B. Of these, **557 rows (203,975,784 B, ~194 MiB) are already `UPLOADED`** (real S3
  blobs exist — leave them, GC will reclaim if unreferenced per retention policy, do not delete
  directly), 1 row `UPLOADING` (in-flight), and **1,955 rows (4,256,102,207 B, ~3.96 GiB) are still
  `PENDING_UPLOAD`** — this is the upload volume the fix avoids if reconciled before any retry.
- OLD SELECTED -> STILL SELECTED: 153,340 rows / 158,793,857,163 bytes (~147.9 GiB), unaffected —
  policy.yaml changed nothing outside these three `erf/media` subtrees.
- `music_risk_review` **proven** (not assumed) present and unaffected: 10 rows, all
  `classification=OUTPUTS`, `upload_status=PENDING_UPLOAD`, 92,452,831 bytes (~88.2 MiB).
- Note: the live `erf/media` tree today (2026-08-23 `du`) is larger than what's in the
  `backup-2026-08-19` scan (e.g. `thumbs` is 309 MiB on disk now vs ~9.7 MiB in the scan) — the
  pipeline kept producing keyframes/thumbs after the 2026-08-19 scan. Irrelevant to whether a rescan is
  *required by this policy fix* (see below), but means the scan is independently stale for `erf/media`
  regardless of the policy question, for whoever eventually decides on a fresh full scan.

**⚠ NEW FINDING — the policy fix alone does NOT change what `backupctl retry` would do.** Traced
`Runner.run_for_date`'s resume path (`app/runner.py`, confirmed via
`tests/test_runner_failed_resume.py::test_run_for_date_resumes_upload_without_wiping_scan_items`):
when `phase=FAILED` and `resume_phase=UPLOADING`, resume **skips SCANNING entirely** and reuses the
existing `scan_items` rows as-is — `classification` is whatever was written at scan time on
2026-08-19, under the *old* (bugged) policy. It is **not** re-evaluated against the current
`policy.yaml` on resume. So fixing `policy.yaml` and then just running `backupctl retry 2026-08-19`
would still upload the 1,955 `PENDING_UPLOAD` keyframes/proxies/thumbs rows (~3.96 GiB) — the stored
`classification=OUTPUTS` on those rows doesn't know about today's fix.
**This means TASK 5's "do not upload the now-excluded items" requirement needs an actual
reconciliation step against `scan_items` (e.g. a one-off reclassification pass using `Policy`,
deleting/repointing the now-`REDOWNLOADABLE` rows, done through the app's own `StateDB`/`Policy` APIs,
not raw SQL against production state) before any retry — not merely the policy.yaml edit.** This is a
genuinely ambiguous design/data decision (mutating a live transaction's `scan_items`) and per this
project's own MODEL section belongs to Daniel/Opus, not a unilateral Sonnet action — flagging rather
than implementing it.

**TASK 6 — targeted conformance audit of other adjudicated/named areas**, real `Policy.classify()`
calls against real representative paths, not string search of docs:
- `.claude/projects`, `.claude/file-history`, `.claude/.credentials.json`, top-level `catalog/`
  (+ `catalog/logs|status|milvus`), `melb_acquire/data`, `erf/catalog`, `_BACKUP/{state,logs,config/
  credentials.env,app,config}`, `footage/RED_OTHER` (PRINCIPAL/excluded) — **all conform**, matched
  expectations, no action needed.
- `table_read/Media_H264/*` and `table_read/Media/*` and `table_read/braw_decode/*` — **all three
  classify UNCLASSIFIED** (reported, not silently dropped — the invariant held) but this means none of
  them are currently selected for backup. `table_read/braw_decode` was noted resolved in BATON.md
  BLOCKED history ("decided to keep the whole directory") but that resolution, like the `erf/media`
  one, was **never written into `policy.yaml`** either. `Media_H264`/`Media` were not previously
  adjudicated at all as far as this session found. Needs Daniel's read: these may be real recorded
  table-read footage, not obviously regenerable — left unfixed and unclassified deliberately rather
  than guessed, per this project's own doctrine.
- `POC_VFX/GeneralAssets/houdini_caches` — **dead rule, found by this audit**: the exact same prefix
  `POC_VFX/GeneralAssets/houdini_caches` is listed under BOTH `paths.critical` (line ~112, with the
  comment "exclude .vdb below despite this") AND `paths.redownloadable` (line ~224, ".vdb simulation
  caches - regenerable"). `_match_path_rule`'s tie-break on equal-length prefixes is "first rule
  encountered wins" (`len(rule.prefix) > len(best.prefix)`, strict `>`), and `critical:` is parsed
  before `redownloadable:` in YAML key order, so the `redownloadable` rule is **dead** — confirmed
  live: `.vdb`/`.hipnc` files under that path both classify `CRITICAL`/selected, never
  `REDOWNLOADABLE`. Direction is over-protection (not data loss) and this bug likely predates and is
  already baked into the current volume-sizing numbers, so it is not urgent, but it is a second
  unencoded/broken ruling. Not fixed in this pass (out of the erf/media task scope) — flagged for
  Daniel.
- `platelock/env` — **new finding, largest one found**: `platelock/env/platewarp/{bin,lib,include,
  share}` is a full Python venv, **8.6 GiB**, sitting under the broad `outputs: - platelock` rule with
  no more specific override, and the directory name `env` is not in `app/scanner.py`'s
  `DEFAULT_REDOWNLOADABLE_DIR_NAMES` (`venv`/`venvs`/`.venv` are there, `env` is not) so scanner-level
  pruning doesn't catch it either. Confirmed live: files under it classify `OUTPUTS`/selected. This
  looks exactly like the `erf/media` and `catalog/` pattern — a regenerable venv fully protected as if
  original work — but is a **much larger** unnecessary-protection gap (8.6 GiB) than anything else
  found this session. Not fixed in this pass (out of the erf/media task scope) — flagged for Daniel as
  the highest-value follow-up.
- "C001 BRAW partial / RECOVERY_PENDING" — searched both the filesystem and this project's indexed
  docs; found no literal path or documented state matching this description (only unrelated
  `footage/RED_OTHER/C001_*` RED-camera files, which correctly classify PRINCIPAL/excluded). Could not
  verify or classify something not found — not fabricated, flagged as unresolved/unlocatable rather
  than guessed.

**TASK 7 — is a new scan required?** **NO**, not by this policy correction. Every change made this
session is `OLD SELECTED -> NOW EXCLUDED` (erf/media keyframes/proxies/thumbs) or `unchanged`
(everything else touched by this audit was read-only verification). Nothing that was previously
excluded/pruned by the old scanner became newly required by the fix, so the existing
`backup-2026-08-19` scan is not invalidated by this fix and does not need a fresh 5.4-hour rescan on
that basis. (The `houdini_caches` and `platelock/env` findings above are pre-existing over-protection,
also not a rescan trigger — if anything they'd shrink the selected set further, not add to it, if
fixed later.)

**Net state: two independent blockers now stand between here and a clean retry:**
1. **-3k, still open:** RunPod S3 gateway `QuotaExceeded` — waiting on RunPod support / the queued
   `mfsgetquota` + 1 MiB `PutObject` decisive test (unchanged this session, see -3k below).
2. **New this session:** the `scan_items` reclassification gap above — `backupctl retry` as it exists
   today will not honour the `policy.yaml` fix on a resumed transaction. Needs a Daniel/Opus decision
   on how to reconcile `backup-2026-08-19`'s existing `scan_items` rows before retry is safe.

**Do not run `backupctl retry 2026-08-19` or `backupctl run` until BOTH are resolved.** `policy.yaml`,
the two new tests, and this checkpoint are the durable record of the `erf/media` ruling — it must not
depend on conversational memory again.

## -3k. Checkpoint 2026-08-21 (RunPod S3 stale-quota diagnosis — root cause isolated to the S3 gateway, not the volume, not our code; no retry until RunPod fixes it)

`backup-2026-08-19` (left `FAILED`/`resume_phase=UPLOADING` at the end of checkpoint -3j's retry)
failed again, this time on a plain single-part `PutObject`, not a multipart error:
`QuotaExceeded: storage quota exceeded; increase your network volume size and retry`, on
`v6a4zjiytb/_osf_backup/blobs/sha256/fb/fb00a5f2...`. Confirmed live via `state.sqlite`
`transactions` row (`error` column has the exact message, `updated_at` 2026-08-21T05:59:54+10:00).
This is a new failure mode, distinct from -3i's `InvalidPart`/524 gateway issues.

**Diagnosed with two independent, entirely read-only vantage points before touching anything:**

1. **Control plane** (RunPod GraphQL API, `myself.networkVolumes` — the newer REST
   `/v1/networkvolumes/{id}` 403'd for this API key, GraphQL worked): volume `v6a4zjiytb`
   ("backup"), EU-RO-1, **size = 300 GB** — confirms the 2026-08-17 100GB→300GB resize is correctly
   reflected in the control plane.
2. **S3 API** (`ListObjectsV2`, full pagination, from the CPU pod via `boto3`): whole bucket =
   **61,879 objects / 89.57 GiB**, 100% under prefix `_osf_backup/`, zero objects anywhere else,
   **zero** outstanding multipart uploads (`ListMultipartUploads`). No stray data, no orphaned
   multipart parts hiding usage.
3. **Direct mounted filesystem** (a second Code instance was, per Daniel, run directly against the
   backup volume's mount): `/workspace` there is `mfs#euro-2.runpod.net:9421` — **MooseFS/FUSE**, a
   shared distributed cluster filesystem, not a dedicated per-volume block device. `df`/`df -i`
   report the whole MooseFS cluster's capacity (2.1P total), not this volume's 300GB allocation, so
   there is no way to read this volume's own quota number from inside the pod — MooseFS quota
   config (if any) lives on the master server and is opaque to standard client-side tools; no
   `mfsgetquota`/`mfssetquota`/`mfstools`/`xfs_quota`-equivalent was available to inspect it
   directly. `du -h --max-depth=2 /workspace` found real usage of ~91.5 GiB (`_osf_backup` 91G +
   `.pod-persist` 331M + `.claude` 185M + negligible), matching the S3-side 89.57 GiB closely, and no
   stray/hidden data.
4. **Decisive test**: a disposable file was written directly to the mounted filesystem (not via S3),
   outside `_osf_backup`, via `dd if=/dev/zero … count=15000` — **14.65 GiB, exit 0, no `ENOSPC`, no
   `EDQUOT`**, completed at full disk speed, pushing total apparent volume usage from ~91.5 GiB past
   ~106 GiB — well beyond the ~93 GiB boundary implied by the volume's old 100GB size. Probe file and
   directory deleted immediately after; `_osf_backup` was not touched by any of this.

**Precise conclusion** (worded carefully per Daniel — the filesystem test proves direct mounted
writes are not stopped at the old boundary; it does **not** let us read RunPod's own hidden
per-volume MooseFS quota metadata directly, since no client-side tooling for that exists on this
pod): the underlying mounted storage accepts writes well beyond the former 100 GB limit, so
`QuotaExceeded` is isolated to **RunPod's S3 gateway / quota-tracking path**, not real occupancy
(89.57 GiB used of a real 300GB volume), not the filesystem, and not a bug in our backup code. This
is strongly consistent with **stale S3-side quota metadata that was never refreshed after the
100GB→300GB resize on 2026-08-17**.

**Backup state — do not touch until RunPod fixes this:**
- `backup-2026-08-19` remains `FAILED`, `resume_phase=UPLOADING`.
- Existing uploaded CAS blobs (89.57 GiB, 61,879 objects) preserved — do not delete.
- Completed scan (`scan_items`, 155,853 rows) preserved — do not rescan.
- Do **not** retry (`backupctl retry 2026-08-19`) until the S3 quota is corrected — it will just hit
  the same `QuotaExceeded` wall.
- Do **not** resize the volume again (400GB etc.) — the control plane already correctly shows 300GB;
  resizing further would not touch a gateway-side tracking bug.

**Next action:** send RunPod support a ticket requesting the S3 gateway's quota tracking be
resynced/refreshed against the volume's current 300GB allocation, citing the four findings above
(control-plane 300GB, S3 89.57 GiB / one prefix / no orphaned multipart, matching filesystem-level
usage, and the clean 14.65 GiB write past the old boundary). Daniel confirmed the drafted ticket text
is good to send essentially as-is. **Once RunPod confirms/fixes it, resume with:**
`BACKUP_HOME=/workspace/_BACKUP .venv/bin/python bin/backupctl retry 2026-08-19` — do not use `run`
(see checkpoint -3i's date-rollover lesson if the Melbourne date has since rolled over again).

**Addendum, same day — follow-up diagnostics on the separate pod with the backup volume mounted
directly:**

- **`runpodctl config` side effect (not read-only, as it was first assumed to be):** running it on
  that pod generated a new SSH keypair as a side effect — a stray private key confined to that
  pod/container, plus an extra public key added to the RunPod account. **Daniel's call: leave it (do
  not spend effort reversing a benign diagnostic side effect); the extra public key can be removed
  from the RunPod dashboard later if wanted. `runpodctl config` must not be used again for this
  investigation** — anything that mutates account/pod state is off-limits for what is supposed to be
  a read-only diagnostic.
- **A subsequent genuinely non-sparse 8 GiB filesystem write also succeeded cleanly** on that mount,
  reinforcing the -3k finding (the earlier 14.65 GiB `dd` write could theoretically have been sparse;
  this one wasn't, closing that gap) — the mounted storage really can allocate real data well past
  the old ~93 GiB boundary.
- **Next, most decisive test queued (not yet run as of this addendum):** from that same backup-volume
  pod, using boto3 (installed locally there if needed) and the project's existing working S3
  credentials, run a minimal, single Phase 4 probe: `PutObject` a 1 MiB random file to a disposable
  key (`_diag_s3_quota_<timestamp>/probe.bin`), and on success `HeadObject`/`GetObject`/sha256-compare/
  `DeleteObject`; on failure capture the full error once, no retry loop. Also queued: install
  **MooseFS client-only** diagnostic tools (`mfsgetquota`/`mfsdirinfo` etc., adding the official
  MooseFS package repo if the distro package isn't available) — client tools only, never
  master/chunkserver, and never a quota-*mutating* command (`mfssetquota`/`mfsdelquota`/
  `mfscopyquota` are explicitly forbidden). Interpretation keyed off the combination: `mfsgetquota`
  ~100GB + 1 MiB PutObject still fails → RunPod's S3 layer is likely not even reading the same quota
  the mount enforces, just feeding it stale metadata; `mfsgetquota` ~300GB/unlimited + 1 MiB PutObject
  still fails → very strong proof of an S3-specific stale quota/accounting bug in the gateway itself,
  independent of MooseFS; 1 MiB PutObject suddenly succeeds → a backend quota refresh may have
  already happened, verify with a somewhat larger disposable object before touching the real failed
  backup. **Explicitly ruled out for now: do not try a 300→301GB resize as a "propagation nudge"** —
  enough evidence exists to diagnose cleanly without another control-plane mutation; if the 1 MiB
  probe still hits `QuotaExceeded` alongside a real multi-GiB filesystem write succeeding, that goes
  straight to RunPod support as-is rather than triggering more experiments.
- Still true, unchanged: `backup-2026-08-19` untouched (`FAILED`/`resume_phase=UPLOADING`), no
  retry, no resize, no cleanup until RunPod actually fixes the gateway quota.

## -3j. Checkpoint 2026-08-20 (Daniel gave explicit GO on `backupctl retry 2026-08-19` against the new UploadPart-retry code — launched, IN PROGRESS as of this checkpoint)

Session resumed after a `/clear`; re-verified real state before acting rather than trusting the
carried-over summary (it was flagged `⚠ SUSPECT`). Confirmed via direct DB query + `ps aux`: no
competing backup process running, `backup-2026-08-19` still `FAILED`/`resume_phase=UPLOADING`
(697/155,853 `scan_items` already `UPLOADED`), `backup-2026-08-20` untouched at `SCANNING` as
checkpoint -3i left it, and the CLI guard (`bin/backupctl` L330-344/714-716) present.

Launched `BACKUP_HOME=/workspace/_BACKUP .venv/bin/python bin/backupctl retry 2026-08-19` via the
harness's own tracked backgrounding (task id `bau5hpwg0`) — not raw `nohup … &`, per the lesson
already burned into checkpoints -3h/-3i. Confirmed alive and correct within ~30s: log reads
*"previously failed during UPLOADING; resuming there (scan_items already complete, not
re-scanning)"*, `transactions.phase` moved `FAILED`→`UPLOADING`, `resume_phase` cleared to `NULL`,
and `UPLOADED` count is climbing against the preserved 155,155-item `PENDING_UPLOAD` backlog. As
before, the backgrounded process is not visible to `ps aux` from an interactive shell — check the
task output file and the `transactions`/`scan_items` tables directly, not `ps`.

**Backup code/config frozen while this run is in flight, per Daniel's explicit instruction** — this
execution is meant to test the exact revision validated at checkpoint -3i (the full
`app/storage.py` UploadPart-level retry hierarchy), so no edits until it reaches a terminal state.

**On resume:** check for the background-task completion notification first; if none has arrived,
query `transactions` (`phase`, `resume_phase`, `error`, `updated_at`) and `scan_items.upload_status`
directly for real liveness — a large `PENDING_UPLOAD` count and no recent `updated_at` movement
means stalled, not necessarily failed; 155k files at real network speed is itself a multi-hour tail
even with resume, not the ~5.4h fresh-scan figure from checkpoint -3h.

**Not started, waiting on this run's real terminal outcome (Daniel's ordering, unchanged from
-3h/-3i):** the full `backup-2026-08-19` audit (state=SUCCESS, `resume_phase` cleared,
manifest/catalog flags, `scan_items` terminal-status breakdown, remote occupancy/object-count/no
orphaned multipart, classification spot-checks incl. `C001`=`RECOVERY_PENDING` and `Media_H264`
present, the 4 ERF SQLite DBs via `.backup()`) → `backupctl verify` → real restore drill → isolated
`rebuild_state` comparison → **only then** reconcile the still-untouched `backup-2026-08-20` row
(let it run a genuine scan/catch-up; do not delete it or let -19's success stand in for it) →
daemon/supervisor/Healthchecks/scheduling re-enablement.

## -3i. Checkpoint 2026-08-20 (real seed hit two distinct RunPod gateway failures, not one — resume logic fixed, full multipart retry hierarchy rebuilt, backup-2026-08-19 currently FAILED, not yet retried against this new code)

**This checkpoint supersedes -3h's "Production assumption" section (lines below, kept for history)
— that section documented one gateway quirk; there are now three, all handled, all documented
together in `app/storage.py`'s module docstring (the authoritative technical detail — this entry is
the narrative).**

**What actually happened to the -3h real seed, in order:**

1. `backup-2026-08-19` completed its ~12.8h SCANNING phase cleanly (752k+ files, matching the
   2026-08-17 dry-run), then failed ~6 min into UPLOADING on `CompleteMultipartUpload: InvalidPart`
   ("part 1 is missing; 2 parts missing") — the gateway lost track of already-acknowledged
   `UploadPart` calls before `Complete` saw them. Confirmed clean, not data loss: no orphaned
   multipart upload on S3, object absent remotely, `manifest_committed=0`.

2. **Real bug found while diagnosing, not hypothetical:** `runner.py`'s FAILED-retry path
   (`run_for_date` → `_reset_transaction`) unconditionally wiped `scan_items` and restarted from
   `PENDING` on *any* retry, regardless of which phase actually failed — so retrying after an
   UPLOADING-phase failure would have forced a full ~5.4h rescan before even reaching the upload
   phase again. **Fixed:** new `resume_phase` column on `transactions` (migrated live against the
   real `state.sqlite`, verified), `_mark_failed` captures the phase that was actually running,
   `run_for_date` only full-resets when that's PENDING/SCANNING (scan state not trustworthy) —
   otherwise `_resume_transaction` resumes directly, leaving `scan_items`/`deferred_items` untouched.
   Backfilled `resume_phase='UPLOADING'` by hand on the already-failed `backup-2026-08-19` row (it
   predates the fix, evidenced by 155,853 real `scan_items` rows + 126 already `UPLOADED`). Tests:
   `tests/test_runner_failed_resume.py` (3 tests).

3. **Operator mistake, self-caught:** relaunched with `backupctl run` instead of `backupctl retry
   2026-08-19` — the Melbourne date had rolled over to 2026-08-20 mid-session, so `run` (which always
   uses `_melbourne_today()`) started a *new* `backup-2026-08-20` transaction and began rescanning
   from scratch instead of resuming the fixed transaction. Caught within ~30s via log tail, stopped
   via the harness's `TaskStop` (the backgrounded process isn't visible to `ps aux` from this
   session's shell — same as the -3h checkpoint's launch, worth remembering each time), relaunched
   correctly with `retry 2026-08-19`. Confirmed via log: *"previously failed during UPLOADING;
   resuming there (scan_items already complete, not re-scanning)."* The abandoned
   `backup-2026-08-20` row (phase=SCANNING, partial scan_items) was left alone rather than deleted —
   `_scan()` always wipes and rebuilds its own `scan_items` from scratch, so it's self-healing
   whenever a real run for that date eventually happens. **Lesson: never use `run` to resume a
   past-date FAILED transaction across a date rollover — always `retry <explicit date>`.**

4. **The `retry 2026-08-19` attempt itself then failed a second time** — resume logic worked
   correctly (126 files still `UPLOADED`, preserved; 155,726 correctly `PENDING_UPLOAD`; no rescan)
   — but on `UploadPart` itself: `ClientError: 524` (Cloudflare-style gateway timeout, empty
   response body), same blob both times
   (`/workspace/BTS/Darby new/PXL_20250920_110144889.mp4`, 426,168,009 bytes,
   `sha256:44278f0f...`). The InvalidPart-only retry from step 2 correctly did not swallow this (it's
   a different error class) — behaved as designed, not a bug.

5. **Investigated before touching anything further, per Daniel's explicit hold:** local blob sha256
   recomputed and verified bit-for-bit intact (rules out corruption). This was the *first*
   multipart-sized file in the whole transaction — every one of the 126 prior successes was a plain
   single-PUT under 128MiB (largest: 79MB) — so multipart's real failure characteristics were being
   exercised for the first time here, not isolated to one cursed blob. No orphaned multipart upload
   left by the 524 either. The existing live multipart roundtrip test
   (`test_s3_transfer_paths.py`, 132MiB/2-part synthetic file, disposable prefix) passed cleanly
   right now. A closer diagnostic — synthetic 426MB/4-part upload, exact size/part-count match,
   disposable prefix — also completed the full multipart sequence
   (`Create`+4×`UploadPart`+`Complete`) successfully, taking **265s (~4.4 min)**; it then hit the
   *already-known, already-mitigated* transient post-Complete `HeadObject` 403 (the diagnostic script
   bypassed the retry wrapper on purpose, so this was expected, not new). **Conclusion: gateway
   flakiness correlated with transfer duration** (real transfers at this size run several minutes,
   a long exposure window), not a fundamentally broken multipart mechanism or a bad file.

6. **Design call (Daniel, given the evidence above): add UploadPart-level retry, at the part level,
   not whole-file-restart-on-every-hiccup.** Implemented in `app/storage.py` (moved here from
   Runner deliberately — Runner now just calls `put_object_from_file(...)` and gets back a verified
   upload or a terminal exception; it no longer needs to know any RunPod transport-level quirk).
   Full design, rationale, and all three quirks documented together in that file's module docstring.
   Summary: a transient `UploadPart` failure retries the *same* part/upload_id/bytes with bounded
   exponential backoff (4 attempts, ~2s/~5s/~12s + jitter); a part that exhausts that budget aborts
   the multipart upload and retries the *whole file* under a fresh upload_id (bounded to 2 whole-file
   attempts); `CompleteMultipartUpload` InvalidPart feeds the same whole-file-retry path; a
   deliberately narrow *allowlist* classifier (408/429/500/502/503/504/524 + a short list of known
   transient S3 error codes + connection-level errors) decides transient vs. permanent — permanent
   errors (AccessDenied, bad credentials, ...) propagate immediately, no retry at any level, no
   pointless whole-file retry either. A part is never considered uploaded without a captured ETag; the
   file is never considered uploaded without `Complete` succeeding *and* a bounded post-Complete
   `HeadObject` size-verification succeeding. Old `client.upload_file()`-delegated implementation
   (and its InvalidPart-only retry from step 2) replaced entirely by a hand-rolled multipart
   sequence for files above the effective threshold; the small-file path is untouched (0/126 failures
   observed there; botocore's own standard retry already covers it).
   Tests: `tests/test_storage_multipart_retry.py` (6 tests, in-memory fake S3 client that genuinely
   reconstructs uploaded objects from parts, so integrity assertions are real, not assumed) — covers
   same-part-retry, part-exhaustion-triggers-whole-file-retry, whole-file-retry-also-exhausted,
   permanent-error-no-retry, InvalidPart-triggers-whole-file-retry, and the HeadObject-after-Complete
   retry. Full suite re-verified after the rewrite: 35 non-live + 2 `test_s3_transfer_paths.py` live
   + 3 `test_runner_integration.py` live, all against the real bucket, all green.

**State at this checkpoint: `backup-2026-08-19` is FAILED, `resume_phase='UPLOADING'`, 126/155,853
scan_items UPLOADED, NOT YET RETRIED against this new multipart-retry code.** Per Daniel's explicit
hold, no code was touched while a retry was in flight, and no retry was launched while this rewrite
was in progress. Next action is `backupctl retry 2026-08-19` (never plain `run` — see lesson in step
3), then Daniel's post-run audit (state=SUCCESS, `resume_phase` cleared, manifest/catalog flags,
`scan_items` terminal-status breakdown, remote occupancy/object-count/no-orphaned-multipart,
classification spot-checks, the 4 ERF SQLite DBs via `.backup()` not raw copy) before proceeding to
`verify` → restore drill → isolated `rebuild_state` comparison → `backup-2026-08-20` reconciliation →
daemon/supervisor re-enable. A `backupctl run` CLI guard (refuse to start a new date while an older
FAILED-but-resumable transaction exists; print the `retry <date>` guidance) was discussed and
deliberately deferred to *after* this audit, not implemented yet.

## -3h. Checkpoint 2026-08-19 (Daniel gave explicit GO — real seed launched, IN PROGRESS as of this checkpoint)

**Daniel's response to checkpoint -3g, same session:** 300GB is the confirmed intended live size —
do not resize down to 250GB; the earlier 250GB figure was superseded once the volume was actually
provisioned, and the extra ~279.4 GiB usable margin is wanted, not wasted. `.claude/.credentials.json`
→ hard_exclude ruling approved as consistent with intended policy. The remaining `doctor` FAIL
(`last successful snapshot age`) is accepted as expected pre-seed bootstrap state, not a blocker —
Daniel would prefer `doctor` eventually label it `EXPECTED`/`WARN: NEVER_STARTED` rather than a
generic FAIL, but explicitly said not to hold the seed for that cosmetic fix (not done this session;
flagged as a real but non-blocking follow-up).

**Final pre-launch checks, run for real immediately before launch, all confirmed:** bucket
`v6a4zjiytb`, prefix `_osf_backup` (the real production prefix), capacity_bytes `300000000000`
(279.4 GiB), `source.root: /workspace`, the real production `config/policy.yaml` (not a test
config), zero existing committed snapshots (`list_snapshots` empty, bucket had 0 objects total),
zero competing backup processes (only the legitimate idle `backupd.py`, pid 36237, `NOT_CONFIGURED`
and not touching S3).

**Real seed launched via `backupctl run` (no special seed path — Daniel's explicit instruction was
not to bypass Runner/StateDB, and none was built).** One real mistake made and caught same session:
first launch used a raw `nohup … &` background detach, which this project's own history (checkpoints
-1/-2/-3b) already documented as unsafe here (gets reaped independently of the harness's process
tracking). Caught within ~15s, killed cleanly before anything was committed or uploaded (verified:
`transactions` table showed only a `SCANNING` phase row, `manifest_committed=0`,
`catalog_uploaded=0`; bucket still 0 objects), and relaunched correctly through the harness's own
tracked background execution. The relaunch correctly resumed the same `backup-2026-08-19`
transaction row rather than erroring or duplicating it — confirms the crash-mid-transaction resume
path (already verified in checkpoint -3d's simulated case) also works for a real, non-simulated
kill.

**As of this checkpoint: the run is genuinely in progress, phase SCANNING, PID 139490 (child of this
session's background task, not tied to any particular chat turn).** The full source tree is
752,659 files / 1,666 GiB per the 2026-08-17 dry-run; that scan alone took ~5.4h on its own, so this
is a real multi-hour operation — a quiet log is expected, not evidence of a hang. **On resume: check
for the background-task completion notification first; if none has arrived, check `ps aux | grep
"bin/backupctl run"` and the `transactions` table's phase/updated_at directly rather than assuming
either success or a hang.**

**Daniel's post-run steps (his 4–8), not started, waiting on this run's real outcome:**
verify committed snapshot + run=SUCCESS + plausible totals against the ~134 GiB protected set + no
PRINCIPAL/redownloadable leakage + `C001` partial = `RECOVERY_PENDING` + `Media_H264` protected +
plausible remote occupancy → `backupctl verify` against the real committed snapshot → a real restore
drill (small authored file, directory/tree, large media/BTS file, git-rescue material, one ERF
SQLite-safe snapshot — validate hashes, and for SQLite, integrity/openability) → a real
`rebuild_state` exercise against real production metadata into an *isolated scratch DB*, never the
live production StateDB → only then re-enable/restart backupd+supervisor, confirm next
Melbourne-midnight scheduling, configure Healthchecks if still outstanding, test a controlled daemon
restart and a controlled pod restart, and confirm today's seed isn't repeated while the next logical
run is correctly the next Melbourne calendar day.

**Production assumption, explicitly flagged by Daniel — do not let this get "simplified away"
later:** RunPod's S3 gateway can 403 a `HeadObject` issued immediately after a multipart
`CompleteMultipartUpload`, before the object is fully resolved server-side. This is real, confirmed
live behavior of the actual endpoint (checkpoint -3g item 3), not hypothetical defensive
programming. `app/runner.py`'s `Runner._head_object_after_upload()` (3 attempts, 2s apart) is the
fix, wrapping the two post-upload HeadObject checks in `_upload()` and `_verify()`. Any file over the
128 MiB multipart threshold can hit this — BTS footage (31.6 GiB / 1,104 files) certainly qualifies —
so a future edit that removes this retry wrapper as "unnecessary complexity" would reintroduce a real
false-failure mode on real seed/nightly runs, not a theoretical one.

## -3g. Checkpoint 2026-08-19 (pre-seed punch list: rebuild_state fixed, two policy rulings locked in, S3 multipart/retry gap closed, doctor's daemon check fixed — CAPACITY MISMATCH FOUND, gate NOT green)

Daniel gave six concrete pre-seed items this session, explicitly: do not start the real seed yet. Status of each:

1. **`app/restore.py`'s `rebuild_state` — fixed and functionally verified, not just made to import.** The old version probed for fictional StateDB method names (`store_snapshot`/`store_blob`/etc, same bug shape restore.py's other functions had before checkpoint -3f) and would have raised `TypeError` on first real use. Rewritten to call the real `StateDB` API: `record_snapshot` + `add_snapshot_entry` + `record_blob` + `bump_refcount` per manifest entry, oldest-snapshot-first (so dedup/first-upload semantics come out right), plus a new `upsert_source_file` call per entry so per-path classification survives a full local-state loss too (that table existed in the schema but had zero writers anywhere in the codebase until now). Also fixed: `bin/backupctl`'s `rebuild-state` command wasn't threading `key_prefix` at all (same missing-prefix bug the other restore.py call sites had before -3f) — now does. New test
   `tests/test_runner_integration.py::test_rebuild_state_reconstructs_from_remote_manifests` (marked `live`): runs two real Runner transactions against the real bucket (disposable prefix), destroys the StateDB, calls `rebuild_state`, and asserts the rebuilt snapshots/blob refcounts/snapshot_entries/source_files exactly match what the original two runs produced (source_files checked against the real manifests directly, since Runner itself still never writes that table — confirmed, not assumed). Passes for real.

2. **Daniel's two classification rulings locked into `config/policy.yaml`, with regression tests:**
   - Top-level `catalog/` → CRITICAL (trivial size vs. capacity, reconstructability from `pipeline/`'s raw inputs unconfirmed). Existing derived-only subdirs (`catalog/logs`, `catalog/status`, `catalog/milvus`) correctly still win via longest-prefix and stay REDOWNLOADABLE.
   - `.claude/projects` and `.claude/file-history` → REDOWNLOADABLE (excluded), alongside the existing `.claude/cache`/`.claude/telemetry`/etc. sibling entries. `.claude/CLAUDE.md`, `.claude/settings.json`, `.claude/settings.local.json` → CRITICAL (the "authored operational material" Daniel named). Everything else under `/workspace/.claude/` (commands, daemon, jobs, plans, session-env, sessions, tasks, backups, various top-level logs/state files) deliberately left unclassified — not named in the ruling, and guessing there would repeat exactly the mistake the audit flagged for top-level `catalog/` before Daniel ruled on it.
   - **Not asked for, added by inference, flagged for Daniel to confirm or override:** `.claude/.credentials.json` → `hard_exclude`. Same class of file as the existing `_BACKUP/config/credentials.env` hard-exclude one line above it in the file; left in the backup payload it would be a real live credential store landing in S3. Called out explicitly in the policy.yaml comment next to it, not silently decided.
   - 5 new tests in `tests/test_policy_regression.py` lock all of the above in (19 total in that file now, all pass).

3. **S3 multipart + induced-failure/retry ambiguity closed for real, not inferred — and this surfaced a real seed-blocking bug, now fixed.** New `tests/test_s3_transfer_paths.py` (2 tests, `live`):
   - Multipart: uploads a real 132 MiB file (just over the 128 MiB threshold) through the real `RunPodStorage.put_object_from_file`, counts real `CreateMultipartUpload`/`UploadPart`/`CompleteMultipartUpload` calls via botocore's event system (not inferred from file size), confirms ≥2 real parts, downloads it back, sha256-verifies, deletes. Independently corroborated by S3's own ETag suffix (`...-2`) on the object.
   - Retry: injects exactly one synthetic HTTP 500 via a `before-send` handler on the real client, confirms botocore's real `retries={"mode": "standard"}` policy (already configured in `RunPodStorage`, previously never actually exercised) retries and the real second attempt lands on the real bucket.
   - **Real bug found while running the multipart test, not hypothetical:** RunPod's S3 gateway can 403 a `HeadObject` issued immediately after a multipart `CompleteMultipartUpload`, before the object is fully resolved on their side — confirmed live: `list_prefix` showed the object immediately, the very next `HeadObject` 403'd once, succeeded ~3s later on retry. 403 is not a botocore-retryable status, so `Config(retries={"mode": "standard"})` never covered it. `app/runner.py`'s `_upload()` (immediately after `put_object_from_file`) and `_verify()` both did exactly this unretried check-and-raise — any real file over 128 MiB (BTS footage certainly qualifies, 31.6 GiB / 1,104 files) would have had a real, live chance of Runner raising `"HeadObject size mismatch after upload"` on a completely successful upload, during the actual seed. **Fixed**: new `Runner._head_object_after_upload()` (3 attempts, 2s apart) wraps both call sites; the pre-upload "does this blob already exist" lookup is untouched (a genuine `None` there is normal, must not be retried into an error). New fast non-live test `tests/test_runner_head_object_retry.py` locks in the retry/give-up behavior in isolation. All 5 new `live` tests plus the 26 non-live tests pass; the real bucket is confirmed back to 0 objects after the run (every disposable test prefix, including one ad-hoc diagnostic script, cleaned itself up; the real `_osf_backup` prefix is untouched).

4. **Target capacity verified for real — MISMATCH FOUND, not silently resolved either way.** `backupctl status`/`doctor` report `279.4 GiB` usable (300,000,000,000 bytes, matching `config/policy.yaml`'s `target.capacity_bytes`). Cross-checked independently via a read-only RunPod GraphQL API call (`myself.networkVolumes`): network volume `v6a4zjiytb` ("backup") is genuinely provisioned at **300 GB**, not the old 100 GB. But Daniel's instruction this session stated the intended target as **250 GB nominal / ~232.8 GiB usable** — that does not match what's actually live. This is *not* the "still on the old 100 GB volume" failure mode the STOP instruction described (300 GB is more capacity than 250 GB, not less) but it is a real mismatch between Daniel's stated expectation and reality, worth exactly as much a STOP by the spirit of his own item 6 ("if... target is actually 250 GB, then the pre-seed gate is satisfied" — it is not actually 250 GB). **Nothing in policy.yaml's capacity_bytes was changed to try to resolve this either direction** — this needs Daniel's explicit call: is 300 GB (matching checkpoint -3f's "resized 100→300GB, Daniel via RunPod dashboard" and the retention.monthly=4 math) the real intended number and his "250 GB" this session a slip, or does the volume actually need resizing down to 250 GB.

5. **`backupctl doctor`: the permanent false-FAIL on daemon/supervisor status is fixed.** The old check hard-required `supervisorctl` or `systemctl` — neither binary exists on this pod, which runs `bin/supervisor.sh` directly (per DEPLOY.md/checkpoint -3b) — so it was structurally impossible for this check to ever pass here, regardless of whether the daemon was healthy. Rewritten to check the *real* liveness mechanism both processes already use: a non-blocking `flock` held for the process's lifetime (`logs/supervisor.lock` for `bin/supervisor.sh`, `state/backupd.lock` for `app/backupd.py`, `DEFAULT_LOCK_FILE`) — if the lock can be acquired, nothing is holding it; if not, a live process holds it (the kernel releases flock() automatically on process exit, even a crash, so this can't false-positive on a stale lock the way a pidfile check could). Verified against live reality, not just made to compile: `ps`/`/proc` fd-scan confirmed PID 36237 (`backupd.py`, 2 days 7h uptime, child of the running `supervisor.sh` pid 31899) genuinely holds `state/backupd.lock`'s fd; the check now correctly PASSes. Full `doctor` run this session: **7 PASS / 1 FAIL**, and the one FAIL (`last successful snapshot age`) is the single documented, expected pre-seed condition (no real run has ever happened) — no unexplained FAILs.

6/7. **Pre-seed gate: NOT satisfied.** Items 1, 2, 3, and 5 are done and real-verified. Item 4 surfaced a genuine discrepancy (see above) that blocks treating the gate as green per Daniel's own stated criteria — needs his explicit confirmation of which capacity number is correct before this checkpoint can be closed out. **No seed action was taken or attempted; the real `_osf_backup` prefix remains untouched, confirmed empty this session.**

## -3f. Checkpoint 2026-08-19 (gates 1/2/3 done: run/retry/verify wired for real, verified live against S3 — READ THIS FIRST)

**Volume resized 100GB -> 300GB** (Daniel, via RunPod dashboard, outside this session). `config/policy.yaml`
`target.capacity_bytes` updated to 300000000000 to match. Retention trimmed `monthly: 6 -> 4` (Daniel's call,
after a 180-day churn check showed 6-monthly needed ~242 GiB against 250GB's ~233 GiB actual; 4-monthly needs
~218 GiB, fits the now-300GB volume with real margin — see prior session's volume-sizing conversation, not
mechanically tracked in this file).

**Daniel's redirect that shaped this session:** initial plan was a permissive adapter layer around `Runner`'s
`Config` to paper over interface mismatches with the real modules. Daniel rejected this after seeing the trace
of six real mismatches — his instruction: fix `Runner` (and friends) to consume the *real* contracts directly,
fail loud on anything unrecognized (no `except Exception: log-and-skip`), and design real Git-semantic rescue
(not adapters, not a naive per-file-or-tarball binary choice). Full transcript of that reasoning is in this
session's conversation, not reproduced here — the important thing for a future session is: **this codebase's
standard is now "exercise the real interface or fail loudly," and any future integration work should match
that**, not build another adapter shim.

**What actually shipped, every item functionally verified against real code (not just written):**

1. **`app/git_rescue.py`**: `_classify` now three-way (`REMOTE_BACKED_CLEAN` / `RESCUE_REQUIRED` / `UNCERTAIN`
   — new). A repo whose `git status` collection itself errored/timed out (`info["errors"]`) is `UNCERTAIN`,
   not folded into `RESCUE_REQUIRED` — the other signals (staged/unstaged/etc) default to "needs rescue" on
   collection failure, so trusting them for semantic packaging would be building artifacts from data known
   to be wrong. `build_rescue_package` routes `UNCERTAIN` straight to `FULL_BACKUP_REQUIRED` (no packaging
   attempt), `REMOTE_BACKED_CLEAN` to no package, `RESCUE_REQUIRED` to the existing (already-built, just never
   wired anywhere — same "generated but not integrated" pattern as `storage.py` earlier) semantic package
   (`status.txt`/`worktree.diff`/`staged.diff`/`untracked_files.tar`/`local_only.bundle`/`metadata.json`).
   **Verified for real** against `erf_data_backup` (38 real dirty files): package = 161MB vs the repo's 6.1GB
   — confirms the whole semantic-rescue-over-tarball design choice concretely. 5 new tests in
   `tests/test_git_rescue_classification.py` (4 unit + 1 real-repo integration), all pass.

2. **`app/runner.py`** — the big one. `PolicyClass` (3-value fictional enum) removed entirely; everything now
   uses the real 10-value `Classification` from `app.policy`. `_check_capacity` maps `Classification` to a
   `HARD_PROTECTED`/`DISCRETIONARY` tier via a new explicit `_capacity_tier()` function (raises on anything
   unexpected — no silent default). Scan loop (`_scan_source`) rewritten: two explicit branches for the
   scanner's real `Candidate`/`RepoCandidate` types (previously fictional `getattr(entry, "path", None)` —
   real `Candidate` has `absolute_path`/`relative_path`, no `.path` at all; this alone would have silently
   skipped every single file forever). `RepoCandidate` -> `_handle_repo_candidate` -> `git_rescue.build_rescue_package`,
   branching on status exactly as designed above; `FULL_BACKUP_REQUIRED` falls back to `_walk_repo_full_backup`
   (per-file, CAS-deduped, **not** a tarball — prunes and separately recurses into nested repos rather than
   sweeping their files under the parent's classification). Any candidate type the scanner doesn't recognize,
   or a classification `_capacity_tier` doesn't recognize, now raises — no more catch-and-log-and-skip.
   `blob_store` calls fixed to the real `RunPodStorage` method names/signatures (`head_object` returns a bare
   `int | None`, not an object with `.size`; `put_object_from_file(key, path)`/`get_object_to_file(key, dest)`,
   not the fictional `put_file`/`download_to_path`). New `_blob_key()`/`Runner._blob_key()` make the remote key
   format (`{prefix}blobs/sha256/{sha[:2]}/{sha}`) the *one* place it's constructed — this fixed a real
   inconsistency where `record_blob`'s stored `remote_key` didn't match what upload/head/download actually used.
   `Config.key_prefix` (new field) namespaces every key Runner writes — was completely unwired before (policy.yaml's
   `target.prefix` was pure decoration); load-bearing for both production key hygiene and test isolation.
   `healthcheck.success()` now builds a real body (`_success_summary`) and is called *after* the transaction is
   already durably `SUCCESS` in StateDB, in its own try/except — a dead Healthchecks.io endpoint can no longer
   flip a good backup to `FAILED` (it could have, before). `run()` split into `run()` (today) + `run_for_date(txn_date)`
   (arbitrary date) so `retry` has a real, non-duplicated code path. `_upload_manifest` now also calls
   `canonical_state.record_snapshot`/`add_snapshot_entry` for every entry (previously never called at all —
   `RetentionRunner` would have had nothing to expire, ever) and `_update_snapshot_index` (new) maintains
   `meta/snapshot-index.json` via real read-modify-write — **this index was never written by anything**, so
   `restore.py`'s `list_snapshots`/`_resolve_manifest_key` (which only ever read it, never listed the blob
   tree, by design) would have found nothing forever, even after a fully successful run. Also fixed:
   `_scan_source`'s `policy.classify()` call was passing an **absolute** filesystem path — `Policy.classify`'s
   path rules are relative to `source.root` (e.g. `"BTS"`, `"erf/catalog"` in policy.yaml) — every single file
   would have classified `UNCLASSIFIED` in a real run. Caught by the integration test, not by inspection.

3. **`app/policy.py`**: new `Policy.sqlite_databases()` method (filters `path_rules` for
   `Classification.SQLITE_DATABASES`, returns source-relative path strings) — this didn't exist; Runner's
   `_scan_sqlite_databases` needed it and there was no real accessor.

4. **`app/statedb.py`**: new `expire_reference(hash)` (atomically decrements refcount and stamps
   `unreferenced_since` **only on the transition to zero** — calling the existing `mark_unreferenced` naively
   every retention pass would have kept resetting the GC grace clock on an already-zero blob, so it would never
   actually become GC-eligible), `delete_blob(hash)`, `delete_snapshot_entries(run_date)` (makes
   `RetentionRunner` idempotent: expiring a run_date deletes its `snapshot_entries`, so a later pass with
   nothing left to expire is a safe no-op, not a double-decrement).

5. **`app/retention.py`**: new `RetentionRunner` (wires `select_retained` to real `StateDB`) and `GCRunner`
   (wires `gc_candidates`/`unreferenced_blobs` to real `StateDB` + `blob_store.delete_object`, remote delete
   before local row drop — a failed remote delete leaves the blob a candidate for retry, never silently
   vanishes locally while still costing storage remotely). The pure functions (`select_retained`,
   `expire_manifest`, `gc_candidates`) are untouched and still independently testable.

6. **`app/restore.py`**: `_read_bytes`/`_list_prefix` were probing for method names (`read_bytes`,
   `download_bytes`, `list_keys`, `list_objects`, ...) that don't exist on the real `RunPodStorage` — **every
   single restore.py function was broken** (`list_snapshots`, `load_manifest`, `verify_snapshot`, `restore`,
   `rebuild_state` all route through these). Fixed to call `storage.get_object_bytes(key)` /
   `storage.list_prefix(prefix, max_keys=...)` directly. `restore()`'s file-restore branch was trusting
   `_entry_blob_key(entry)`'s raw manifest field (usually literally the bare sha256 string) as a storage key —
   it isn't; fixed to construct the real `{prefix}blobs/sha256/{sha[:2]}/{sha}` key from the resolved sha256,
   same as everywhere else. `list_snapshots`/`_resolve_manifest_key`/`load_manifest`/`verify_snapshot`/`restore`
   all gained a `prefix` parameter threading through to match `Runner.Config.key_prefix`. New `verify_snapshot()`
   + `VerifyReport` — the standalone verify entry point outside the live transaction phase machine gate 2
   needed; HeadObject-checks every blob a committed manifest references, optionally downloads+hashes a sample
   up to `sample_max_bytes`. **Found, not fixed (deferred, out of this session's scope, flagged so it isn't
   mistaken for done):** `rebuild_state`'s `_statedb_store_snapshot`/`_statedb_store_blob` probe for StateDB
   method names (`store_snapshot`, `upsert_blob`, ...) that don't exist either — real methods are
   `record_snapshot`/`record_blob` with completely different signatures (7 args vs 2, etc). `backupctl
   rebuild-state` will fail the same way restore.py did before this session's fixes. Same bug shape, not yet
   addressed.

7. **`app/storage.py`**: new `put_object_bytes(key, data)` (symmetric with existing `get_object_bytes`) —
   needed for `_update_snapshot_index`'s read-modify-write.

8. **`bin/backupctl`**: `run`/`retry`/`verify` wired for real (were honest stubs). New `_build_runner()`
   constructs a real `Runner` from real modules + a small real `_CapacityTracker` (current usage from
   `StateDB.total_repository_bytes()`, thresholds from `target.capacity_bytes` x
   `storage.{hard,discretionary}_stop_fraction` — both already in policy.yaml, previously unused) + real
   `RetentionRunner`/`GCRunner`. New `_key_prefix(raw_policy)` normalizes `target.prefix` and is threaded into
   every `app_restore.*` call site (`snapshots`/`verify`/`restore` commands) so they agree with what `run`
   writes.

9. **`config/policy.yaml`**: `sqlite_databases:` populated with erf's 4 live DBs (was empty — Daniel's explicit
   ruling this session: "protect it, but enforce SQLite-safe backup... regardless of Git classification"; these
   route through `Runner._backup_sqlite`'s real `sqlite3 .backup()` API, never a raw copy). `POC_VFX/Shots/*/plate`
   principal-exclusion glob fixed (was dead — literal-prefix matcher doesn't support `*`, fixed in `policy.py`'s
   `_matches_prefix`/added `_validate_rule_parts` to fail loud on any future unsupported glob instead of silently
   compiling a dead rule) plus the capitalized `*/Plate` variant and two shapes no glob reaches
   (`drop_masks/Workplates`, `drop_into_frame/C003_C002_0924EJ.mov`) plus 4 file-level overrides for
   non-duplicate content sitting inside plate-named dirs (longest-prefix-wins is a **strict length** comparison
   with no literal-over-wildcard tiebreak — a same-length override silently loses a tie, discovered while
   verifying this exact fix; the 4 overrides are one dir level deeper specifically to win outright).
   `outputs:` gained explicit overrides. `target.capacity_bytes` -> 300GB (see top). `retention.monthly` -> 4.
   Full new `tests/test_policy_regression.py` cases lock in all of this (14 total in that file now).

**Real, verified end-to-end**: `tests/test_runner_integration.py` (marked `live`, excluded from default
`pytest` runs via new `pytest.ini` — this project had no pytest config file before this session, despite
`markers`/`addopts` being referenced in an earlier checkpoint's prose; that prose was describing an aspiration,
not a real file. `pytest.ini` now exists and is real). Two tests, both passing against the real bucket with a
disposable, self-cleaning `_test_runner_<uuid>/` prefix: a basic run+round-trip-through-restore.py test, and
the exact gate-1 scenario Daniel specified (commit{A,B}->refcounts; commit{A,C}->A=2,B=1,C=1; expire->A=1,B=0,C=1
with `unreferenced_since` stamped not deleted; idempotent re-run; real GC deletes the object from S3 and drops
the local row). Run manually: `.venv/bin/python -m pytest tests/test_runner_integration.py -m live -v` (~24s,
hits real S3, self-cleans).

`backupctl doctor` now: 7 PASS / 2 expected FAIL (daemon not running under supervisor — not deployed yet;
no successful snapshot yet — no real seed has run). Both FAILs are the documented, expected pre-seed state.

**Not done this session, explicitly still open:**
- Real seed against the actual 1.6TB source tree has **not** been run. Nothing this session touched the real
  `_osf_backup` prefix or uploaded real project data — every real-S3 exercise used a disposable, self-cleaning
  test prefix. Daniel's dependency order (his words, this session): policy/matcher/retention decisions (done)
  -> wire real transaction path (done) -> real integration tests (done) -> disposable S3 tests (done, folded
  into the same integration tests above rather than a separate PUT/HEAD/GET/DELETE-only exercise — the gate-1
  test IS a real multi-object S3 exercise, just structured around the actual Runner rather than raw boto3 calls)
  -> resize to 300GB (done, by Daniel) -> **doctor -> seed -> restore verification -> production enablement,
  none of which have happened yet.**
- `rebuild_state`'s StateDB-method-probing bug (found, described above, not fixed).
- Whether the disposable-prefix test above actually satisfies Daniel's originally-specified "PUT/HeadObject/Get+hash/Delete,
  multipart, retry/failure path" S3 exercise list precisely, or whether he wants a more surgical raw-boto3-level
  test in addition — the gate-1 test exercises Put/Head/Get(download)/Delete and real multi-day multi-object
  state, but doesn't specifically exercise a forced multipart upload (>500MB, `HARD_MULTIPART_UPPER_BYTES`) or
  an explicit induced-failure/retry path. Worth asking rather than assuming either way.
- `.gitignore`/whether any of this is committed anywhere — this project has no git repo at all
  (`git: not a repo` per every session's Tier-0 reality check), so none of this is version-controlled beyond
  living on disk at `/workspace/_BACKUP`.

## -3e. Checkpoint 2026-08-17 (classification-policy rework in response to the -3d audit)

Daniel's response to the -3d audit landed this session: stop sizing the backup volume around the
old fixed 100GB assumption. Order is now classify correctly → measure the resulting natural
protected set → model change-rate/versioning overhead → add headroom → THEN size the volume.
Capacity is an *output* of the policy audit, not an input to it. Full detail, exact policy.yaml
diffs, every path decision and why, and the exact resume commands/paths for the in-flight
full-tree classification run are in **`/workspace/_BACKUP/BATON.md`** (NOW/IN-FLIGHT/BLOCKED
sections) — read that alongside this checkpoint, it has the live state this file doesn't
mechanically track. Short version:

- Fixed two real bugs blocking correct classification: (1) three glob-style entries in
  `policy.yaml` (`"**/__pycache__"` etc) were silently non-functional — `policy.py` only does
  literal prefix matching, no wildcards — fixed via `scanner.py`'s `DEFAULT_REDOWNLOADABLE_DIR_NAMES`
  instead; (2) `git status`/`git remote` were failing "dubious ownership" repo-wide, which meant
  `app/git_rescue.py` (built for exactly the "custom_nodes → git-aware" need flagged in -3d's
  audit) could not run at all — fixed with `git config --global --add safe.directory '*'`.
  process wide, not just this repo.
- Wrote `_audit_20260817/classify_breakdown_git.py` (persistent, reuses the real `Policy`/`scanner`/
  `git_rescue` modules) to get per-file-size, per-prefix, per-age-bucket, git-aware breakdowns that
  `backupctl dry-run`'s own report doesn't produce (no per-file size for classified buckets, and
  `_classify_source_tree` explicitly skips git-aware resolution — still true of production
  `backupctl` after this session, only the standalone audit script has it wired up).
  Full-tree run backgrounded; see BATON.md NOW for exact PID/paths/resume instructions.
- Removed `outputs.max_total_bytes`/`max_root_bytes` (the 25GiB/12GiB caps -3d flagged as starving
  `erf/catalog` behind unrelated project dumps) as capacity-control mechanisms — kept
  `max_file_bytes` only as an oversize-review flag, not a budget.
- ~40 new `policy.yaml` path rules resolving most of -3d's 146,269 unclassified files (comfy/
  workflows+input+output, `_BACKUP` curating itself, various redownloadable exclusions). Two
  ambiguous buckets deliberately left unclassified rather than guessed (data-loss risk either way):
  `erf/state` (live SQLite mixed with legitimate non-DB data) and top-level `catalog/` (legacy
  pre-ERF pipeline output, reconstructable-or-not depending on whether `pipeline/`'s inputs are
  kept). See BATON.md BLOCKED for full reasoning.
- **Report to Daniel not yet written** — waiting on the backgrounded classification run to finish.
  Still stopped before seed/resize, per Daniel's explicit instruction.

## -3d. Checkpoint 2026-08-17 (statedb rework + shebang fix + real credential wiring)

**Two blockers below (checkpoint -3c and §6) were stale — corrected here per Daniel, 2026-08-17:**
- **S3 credentials are NOT blocked on Daniel.** They were already supplied and stored at
  `config/credentials.env` on 2026-08-15 (see old checkpoint 0). Daniel confirmed this session
  they're authorised for persistent storage/use and should not be asked for again.
- **`/workspace/_system/backup/` is NOT a location to write anything to.** It doesn't exist on disk
  and was already explicitly rejected as a second-copy location back in checkpoint 0 (2026-08-15) —
  `/workspace/_BACKUP` is the one real install root. A later user brief asking for credentials there
  was based on stale info; corrected.
- **Healthchecks.io is NOT a blocker for dry-run, S3 testing, or implementation** — only for
  production monitoring. `BACKUP_HEALTHCHECK_URL` stays unset for now; this is expected and fine
  (`healthcheck.py` no-ops cleanly on an unset URL, confirmed by `backupctl doctor`).

**Done this session, all functionally verified (not just written):**

1. **`app/backupd.py` fully reworked to use `app.statedb.StateDB` as the sole run-state authority.**
   The old private-JSON-in-the-lock-file design is gone entirely; `state/backupd.lock` is now a pure
   fcntl mutex with no content. Phase vocabulary now matches `app.statedb`'s documented contract and
   `app.runner.Phase` exactly (`PENDING/SCANNING/UPLOADING/VERIFYING/COMMITTING/POST_VERIFY/SUCCESS/
   FAILED`), plus one daemon-local addition: `NOT_CONFIGURED`.
   - **This closes a real, live bug Daniel flagged as urgent**: the *old* code, with no
     `backup_command`/`verify_command`/`finalize_command` configured (today's actual state), would
     walk through all phases as no-ops and still write `STATUS_SUCCESS` at the end — i.e. tonight's
     scheduled midnight run would have recorded a false "successful backup" of zero bytes. The new
     code checks `hasattr(app.runner, "run_today")` (confirmed still absent — gate 2 has not landed)
     and if it's missing, records `NOT_CONFIGURED` and stops; it is structurally impossible for a
     no-op cycle to reach `SUCCESS` now (the only code path that sets `SUCCESS` would come from a
     real `app.runner.run_today`, which doesn't exist).
   - Legacy generic `backup_command`/`verify_command`/`finalize_command` subprocess hooks were
     **removed**, not preserved — grepped first to confirm nothing else references them (only this
     file and old HANDOFF prose did); keeping a second, untested execution path alongside the real
     `app.runner` design would have been exactly the kind of duplicate-authority problem gate 4
     already warns about.
   - **Functionally verified** against an isolated scratch `state.sqlite` (not the live one) for all
     four state-machine branches: fresh run → `NOT_CONFIGURED`; re-run same day → immediate no-op;
     simulated `FAILED` → waits for retry/midnight, `--once` doesn't retry; simulated crash
     mid-transaction (`phase=SCANNING`) → resumes correctly. Queried the scratch DB after all four:
     zero `SUCCESS` rows.
   - **Deployed live**: `kill -TERM` the running daemon (pid 32060) — same restart mechanism proven
     safe in checkpoint -3b. Supervisor restarted it clean (new pid). Confirmed via `backupd.log` and
     `backupctl status`: today (2026-08-17) now correctly shows `NOT_CONFIGURED`, not
     `NEVER_STARTED`/`SUCCESS`. Tonight's 00:00 Melbourne trigger will go through the same safe path.
2. **`bin/backupctl`'s shebang fixed permanently**: `#!/usr/bin/env python3` → hardcoded
   `#!/workspace/_BACKUP/.venv/bin/python` (same pattern `supervisor.sh` already uses for
   `PYTHON_BIN`). `./bin/backupctl status` now works directly with no `.venv/bin/python` prefix —
   verified functionally, not just that the file changed. Safe to hardcode: `.venv`'s interpreter
   resolves under `/workspace/.pod-persist/uv-python/...` (persistent, not `/usr/bin`), and
   `restore-venv.sh` + `requirements.lock.txt` already exist to survive a rebuild.
3. **`app/storage.py`'s `_load_credentials` now falls back to `config/credentials.env`** when
   `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` aren't already in the environment — this was the TODO
   flagged since 2026-08-15 (old gate-3 item). Verified functionally with **no AWS env vars set**:
   `RunPodStorage` constructs successfully from the file alone. Values are never logged; permissions
   on `config/credentials.env` tightened to `600` (was world-readable `644`).
4. **Real S3 authentication — RESOLVED same session.** Original pair failed `SignatureDoesNotMatch`.
   Diagnosed properly before asking Daniel to regenerate: system clock matched the endpoint's own
   `Date` header exactly (not a clock-skew issue); a minimal boto3 probe using RunPod's exact
   documented client construction (explicit keys, `region_name="EU-RO-1"`,
   `endpoint_url="https://s3api-eu-ro-1.runpod.io/"`, bypassing `app/storage.py` entirely) reproduced
   the identical failure — confirming it wasn't an application bug. Daniel then supplied a fresh key
   pair (same `user_.../rps_...` format RunPod documents); written into `config/credentials.env`
   (mode `600`) and verified functionally through the real `app/storage.py` code path — real
   `list_objects_v2` against `v6a4zjiytb` now authenticates and returns (bucket is empty, as
   expected — nothing has ever been uploaded). `backupctl doctor` now PASSes S3 credentials + S3
   auth/bucket-visible; the two remaining FAILs (`daemon and supervisor status`,
   `last successful snapshot age`) are pre-existing and expected — the harness-managed
   `supervisor.sh` isn't `supervisorctl`/`systemd`, and no real run has completed yet (gate 2).
5. **Full dry-run against the real tree COMPLETED 2026-08-17T16:14:59+10:00.**
   `reports/dry-run-20260817T161459.json` (34MB — parse with a script, don't `Read` it whole).
   Took ~5.4h (19,419s), not the ~16h naive-linear estimate — because the real tree is
   **752,659 files / 1,666 GiB total, not the ~2.3M-file/2,800GB figure quoted elsewhere in this
   doc and in project memory** (that number was stale/wrong — correct it wherever cited).

   **Audit brought to Daniel same session, findings below — still waiting on his go/no-go past
   this gate, nothing wired past dry-run yet.**

   - **Occupancy: 70.63 GiB selected / 93.13 GiB capacity = 75.8%.** BTS 31.60 GiB (all, exempt
     from caps) + CRITICAL 14.03 GiB (all, no cap) + OUTPUTS 25.00 GiB (**capped** — see below).
     PRINCIPAL (1,231.68 GiB) and REDOWNLOADABLE (245.08 GiB) correctly 0 selected.
   - **UNCLASSIFIED: 146,269 files / 52.90 GiB.** ~59% of that count is `comfy/` (`ComfyGeneral`
     54,560, `ComfyUI_LTX23` 13,984, `ComfyUI_WANVACE` 9,144, `ComfyUI_VideoMaMa` 3,871,
     `ComfyUI_SCAIL2` 3,089, `.venv_submit` 2,117) — **not an imperfect split, comfy/ has almost no
     policy path coverage at all** beyond `*/models` (→ redownloadable) and three curated dirs
     (`opensourcefilms-comfyui-workflows`, `_tools`, `workflows` → critical). Second-biggest chunk:
     `catalog/` + `erf_data_backup/catalog` ≈ 34,000 files, also unmapped. Smaller unmapped gaps:
     `erf/state` (4,500), `quarantine/*` (~6,750), `.claude/*` (~3,500, own tool metadata),
     `deepseek_agent/logs` (1,924), `erf/vendor` (436), `melb_acquire/audit_bundle` (261),
     `table_read/_braw_toolchain` (234, despite HANDOFF's old "defaulted table_read to critical"
     note — that default isn't actually in `policy.yaml`'s `paths:` for this subdir).
   - **Structural issue found, bigger than the three questions asked**: `outputs.max_total_bytes`
     (25 GiB) is a **single global cap shared across erf's own production outputs AND four
     unrelated whole projects** (`bikematch`, `platelock`, `plate_reg_lab`, `website` are all
     mapped wholesale to `outputs` in `policy.yaml`). Consequence: even erf's own outputs are
     losing the `newest_first` race — `erf/catalog` 15,269 rejections, `erf/index` 7,677,
     `erf/media` 2,523 — because `website/DL_Dump` (28,861 rejections), `website/recovery`
     (13,310), and a wetransfer dump (14,279) are newer. Total demand across everything mapped to
     `outputs` is 90.98 GiB against the 25 GiB budget (~27% gets in), while capacity itself has
     22.5 GiB of headroom unused (93.13 − 70.63). **Needs Daniel's call**: raise the cap, or give
     each project its own output sub-budget instead of one shared pool.
   - **POC_VFX per-root 12GiB cap on `Shots` correctly working as designed** (108 rejections,
     `newest_first`). Not a concern, matches policy intent.
   - **Unprompted finding**: the scan walks into `_BACKUP`'s own live files (state DB, logs, and —
     correctly hard-excluded, never selected — `credentials.env`) since `_BACKUP` lives inside
     `/workspace`, the scan root. Nothing wrong happened, but worth a deliberate call: exclude
     `_BACKUP/` from its own scan scope entirely, or intentionally keep protecting its own
     config/state (minus live sqlite/lock files) for disaster recovery.

   **Do not proceed past dry-run without Daniel's audit response** — gate sequence in checkpoint
   -3d above is unchanged.

## -3c. Where things stand right now (historical — superseded by -3d above for credentials/healthchecks)

- **`supervisor.sh` + `backupd.py` are live on this pod** (pids 31899/32060
  as of session end) via the sentinel-delimited stanza now installed in
  `/workspace/.pod-persist/bootstrap-claude-pod.sh` — they should survive a
  pod restart automatically going forward. `backupctl status` reports
  `NEVER_STARTED`, next run `2026-08-18T00:00:00+10:00`, `0 B / 93.1 GiB`.
  This is expected and safe: `backupd.py` still runs its pre-`StateDB`-rework
  no-op phases (see checkpoint -3b for why that's confirmed harmless), so
  nothing has actually backed anything up yet.
- **Next concrete tasks, in order:** (1) the `backupd.py` → `statedb.py`
  rework (open since 2026-08-15, still not started — do this before wiring
  any real command into `backup_command`/`verify_command`/`finalize_command`);
  (2) get Daniel's S3 keys + healthchecks.io URL into the pod environment
  (§6, unchanged); (3) re-run the full `dry-run` against the real 2.8TB tree
  now that the `scanner.py` venv-pruning gap is fixed (checkpoint -3);
  (4) the four gates (checkpoint -2/-1), still fully open, nothing has
  advanced past dry-run.
- **Verify before trusting, every time:** `./bin/backupctl <cmd>` needs the
  explicit `.venv/bin/python` invocation on this pod (its shebang resolves to
  system `python3.8`, which lacks `zoneinfo`) — see checkpoint -3b.

---

## -3. Checkpoint 2026-08-16 (scanner pruning + progress logging — pod `4784cl0vka11ge`, no GPU, 4 vCPU, likely the CPU pod)

- **`scanner.py`'s venv-pruning gap (flagged in checkpoint -2 below) is fixed.**
  `DEFAULT_REDOWNLOADABLE_DIR_NAMES` now also includes `venv`, `venvs`,
  `.venv`, `.cache`, `weights`, `.pod-persist`, `models_shared` (previously
  only `camera_rushes`/`camera-rushes`/`redownloadable`/`downloads`/`cache`).
  **Functionally verified**, not just reasoned about: a real `scan()` run
  against `/workspace/_BACKUP` initially still walked straight into `.venv`
  (4066 log lines touching it, thousands of files) because the fix as first
  drafted added `venv`/`venvs` but not the dotted `.venv` — **the same
  leading-dot gotcha already on record for `.cache` vs `cache`** in
  [[backup-project-shape]], just missed on the first pass and only caught by
  actually running it. Re-verified after adding `.venv`: second real run over
  the same tree yielded 0 files from `.venv` (vs. thousands before). This is
  still name-based pruning only, not policy-integrated — the full
  `comfy/models_shared`-style path-scoped pruning remains open per checkpoint
  -2's note ("required before nightly runs are viable").
- **Added periodic progress logging to `scan()`**, per Daniel's ask ("basic
  logging so we can probe where the tools are up to"). New module constant
  `PROGRESS_LOG_INTERVAL_SECONDS = 30`; new keyword-only
  `progress_interval_seconds` param on `scan()` (default 30s, doesn't break
  existing callers). Throttled via `time.monotonic()`, emits
  `logger.info("Scan progress: elapsed=…s dirs=… files=… repos=… bytes=… path=…")`
  after dirs visited / files yielded / repo candidates yielded — never more
  often than the interval. **Functionally verified**: real run with
  `progress_interval_seconds=0.0` showed a log line firing on every
  dir/file event as expected; real run with the default-ish `2.0s` interval
  over the same small tree showed correctly throttled output. Not yet
  observed over a multi-hour run — that's what the next full dry-run will
  prove.
- Both changes were drafted via `ds_flash.sh patch_draft` (required by the
  edit-size hook for `scanner.py`, >200 lines) as a unified diff, reviewed
  against the real file content, then applied as targeted `Edit`s (not a raw
  `patch` apply — the draft's hunk line numbers weren't trustworthy enough to
  apply mechanically). `ast.parse` syntax check clean; file is now 381 lines.
- **Not done this session:** the "second checkpoint -2 item" — following
  `docs/DEPLOY.md` from step 0 (S3 creds / healthchecks.io URL / capturing
  the CPU pod's Container Start Command). Still blocked on Daniel per §6.
  **Do not re-launch `dry-run` or proceed into any of the four gates without
  Daniel's go-ahead** — gate sequence is unchanged and still fully open.

---

## -3b. Checkpoint 2026-08-16 (DEPLOY.md steps 1-2 on pod `4784cl0vka11ge` — STOPPED before step 3, needs Daniel's go-ahead)

**Confirmed this pod (`4784cl0vka11ge`, name "filesystem", no GPU, 4 vCPU) is the
target CPU pod** — matches `$RUNPOD_POD_ID` and is the only `RUNNING` pod on
the account; the GPU pod `43dv6r0ksogovx` referenced in checkpoint -2 now
shows `desiredStatus: EXITED` (confirms Daniel's pod closure went through).

- **One of the three §6 blockers is resolved without needing Daniel**: used
  the `RUNPOD_API_KEY` already present in this pod's environment (read-only
  GraphQL query to `api.runpod.io`, not a credential write/rotation) to fetch
  this pod's `dockerArgs` directly — it is `null`/empty. Per DEPLOY.md step 2,
  an empty Container Start Command means **the image `CMD` is in force and
  there is nothing to preserve** — this item is captured, no console trip
  needed. **AWS S3 keys and the healthchecks.io URL are still genuinely
  Daniel-only** (§0 confirmed still `MISSING` in the pod environment).
- **DEPLOY.md step 1 (verify install) passed**, but its documented command
  (`./bin/backupctl doctor`, relying on the `#!/usr/bin/env python3` shebang)
  **does not work on this pod** — system `python3` here is 3.8.10 (no
  `zoneinfo`, added in 3.9). Had to invoke explicitly as
  `BACKUP_HOME=/workspace/_BACKUP ./.venv/bin/python bin/backupctl doctor`
  (matches the working invocation already on record in checkpoint -2). DEPLOY.md
  step 1's command line should probably be corrected to the explicit venv
  form — flagging, not yet edited. `doctor` result: 6 PASS / 4 FAIL, every
  FAIL the already-documented expected blocker (S3 creds x2, daemon not
  running, no successful snapshot yet) — matches checkpoint -2 exactly, no
  regression.
- **DEPLOY.md step 2 (read-only discovery) found two real, previously-untested
  bugs in `bin/install-startup.sh`, both fixed:**
  1. The script's shebang is `#!/bin/sh`, and this pod's `/bin/sh` is `dash`,
     which does not support the bash-only `set -o pipefail` the script used
     on line 4 — first real invocation (`--dry-run`) crashed immediately with
     `set: Illegal option -o pipefail`. **Worse: the heredoc template the
     script writes out as the new `pod-entrypoint.sh` (i.e. the new Container
     Start Command, if apply had been run) had the identical bug** — had this
     been applied blind, the pod would have failed to boot into the
     supervisor/previous-command chain, exactly the failure mode DEPLOY.md's
     own top-of-file warning is about. Fix: removed both `set -o pipefail`
     lines; nothing in the script actually depends on pipefail semantics (no
     pipeline whose downstream exit status is checked), so this is a pure
     bug fix, not a behavior change. `sh -n` (dash) syntax-checks clean now.
  2. `SUPERVISOR_BIN` defaulted to `${BACKUP_BIN}/backup-supervisor.sh`,
     which does not exist — the real file is `bin/supervisor.sh` (confirmed
     via `ls bin/`). This is the same "components don't agree on a name"
     shape as the other interface-mismatch bugs already on record for this
     project. Fixed the default to `bin/supervisor.sh`; re-ran `--dry-run`,
     the `-x` guard warning is gone and the generated stanza now correctly
     references the real, executable `bin/supervisor.sh`.
  - Both fixes are small (2-line and 1-line) and were applied as direct
    `Edit`s (not through `ds_flash.sh patch_draft`, despite the file being
    324 lines — the edit-size hook did not block direct edits here, only an
    earlier attempt to `Read` a >100-line window of a different >200-line
    file did; noting this in case the gate behaves differently next time).
- **Real `--dry-run` output on this pod** (safe, applies nothing) shows
  `install-startup.sh` would select **`/workspace/.pod-persist/bootstrap-claude-pod.sh`**
  as the persistent hook to append the supervisor stanza to — this is a
  **shared, non-`_BACKUP`-specific pod bootstrap script** (other candidates
  found under `/workspace`: `_worker_bootstrap/bootstrap-heavy-comfy-worker.sh`,
  `comfy/start_comfy_general.sh`, `plate_reg_lab/start_comfy.sh`,
  `start_copyparty.sh` — none `_BACKUP`'s own). Appending to it is reversible
  (sentinel-delimited block, documented rollback in DEPLOY.md's own Rollback
  section) but does affect shared pod-boot infrastructure beyond this
  project. **Stopped here deliberately — did not run `./bin/install-startup.sh`
  (apply mode) or anything past it.** Next session: get Daniel's explicit
  go-ahead before applying, given it edits a shared file outside `_BACKUP`.
  Everything else in DEPLOY.md step 3 onward (`supervisor.sh` bring-up,
  `backupctl status`, the dry-run against the real 2.8TB tree, all four
  gates) is still fully open, unchanged from checkpoint -2/-3.

**Update, same session — Daniel approved proceeding: `install-startup.sh --apply` run.**
Backed up `/workspace/.pod-persist/bootstrap-claude-pod.sh` to
`/workspace/_BACKUP/tmp/bootstrap-claude-pod.sh.pre-install-startup.<timestamp>`
first (that dir/file is new — `/workspace/_BACKUP/tmp/` didn't exist before,
now holds this one backup). Applied; diff against the backup is exactly the
previewed 6-line sentinel-delimited stanza, nothing else touched. `sh -n`
syntax-checks clean post-apply. Re-ran `--apply` a second time to confirm
idempotency: correctly detected the sentinel and reported "no change needed."
**Update, same session — Daniel approved continuing: DEPLOY.md steps 4-5 done.**
Before starting the daemon, checked `app/backupd.py` for what it would
actually attempt: it's still the pre-`statedb.py`-rework version noted as
"not started" since checkpoint 0 (2026-08-15) — private JSON state, no
import of `runner`/`statedb` at all. Confirmed **safe to run anyway**:
`backup_command`/`verify_command`/`finalize_command` all default to `None`
in `backupd.py`'s `DEFAULT_CONFIG` and nothing in `config/` (`policy.yaml`,
`credentials.env`) sets them, so each daily transaction currently cycles
through no-op phases (logs "no command configured; skipping") and writes
only its own local JSON state — no S3 calls, no interaction with the real
`StateDB`, nothing that could conflict with the dual-authority concern in
gate 4. **The `backupd.py` → `statedb.py` rework is still genuinely
outstanding** and must happen before any real command gets wired into these
phase hooks — do not configure `backup_command` etc. until that rework
lands.
- Started via the harness's own backgrounding (not raw `nohup … &`, which
  this project's own trap notes gets reaped). `supervisor.sh` (pid 31899)
  and `backupd.py` (pid 31904) both came up clean;
  `logs/supervisor.log` shows the lock acquired and the daemon start banner.
  `backupctl status` (via the explicit `.venv/bin/python` invocation, see
  above) showed real, sane numbers: `NEVER_STARTED`, next run
  `2026-08-18T00:00:00+10:00` (next Melbourne midnight), `0 B / 93.1 GiB`.
- **Restart survival proven for real**: killed daemon pid 31904 directly.
  Supervisor logged the clean SIGTERM exit (code 0, ran 24s — above the 10s
  immediate-exit threshold, so treated as a normal exit), backed off 10s per
  its own logic, and restarted a fresh daemon (pid 32060) automatically.
  `backupctl status` unchanged/sane afterward.
- **Not done**: the pod-level restart from the RunPod console (DEPLOY.md
  step 5's second half — "one controlled pod restart"). That restarts the
  whole pod, not just this daemon, and could affect other things running on
  it — did not do this without Daniel explicitly asking for it. Still open,
  along with DEPLOY.md step 6 (healthchecks.io config, blocked on the same
  missing URL) and all four gates.

---

## -2. Checkpoint 2026-08-16 (pod migration — READ THIS FIRST if you're a new session on the CPU pod)

- **This session ran on GPU pod `43dv6r0ksogovx`** (a *different* GPU pod than
  the original build pod `dokilqcrpplxs3` referenced above — pod IDs aren't
  stable across RunPod restarts, don't rely on a specific one). Daniel is
  closing this pod down. **Everything below in this checkpoint happened
  before that closure; nothing here has been run on the CPU pod yet.**
- **The full `backupctl dry-run` against the real 2.8TB/2.3M-file source
  root (started ~10:25 UTC, PID 120467) was abandoned unfinished** — Daniel's
  explicit call, made knowing it would be lost, rather than delaying the pod
  closure. After ~2h54m elapsed it had made almost no real progress (738 read
  syscalls total, `wchan=request_wait_answer` — blocked on MooseFS metadata
  latency almost the entire time) and was still stuck inside a single venv
  (`/workspace/comfy/ComfyUI_LTX23/venv/...torch/include/ATen/ops`, 75,568
  files in that subtree alone). No report was written (temp-file-then-replace
  means an incomplete run leaves nothing under `reports/`) and
  `logs/dry-run-manual.log` is empty — there is nothing to resume, no partial
  state to reconcile. Confirmed this was not a fluke: `scanner.py`'s
  `DEFAULT_REDOWNLOADABLE_DIR_NAMES` prune list does **not** include
  `venv`/`venvs`, so the scan fully descends into every Python venv on the
  volume before classification even runs. **Fix this pruning gap before
  re-running `dry-run` on the CPU pod** — otherwise the same run will burn
  hours walking venvs/site-packages it's going to exclude anyway. (Scanner
  has no periodic progress logging either — only `logger.warning`/`debug` on
  errors and prune decisions — so a re-run will look silent/stuck by design
  until it either errors or finishes; that's not new breakage.)
- **Next session (on the CPU pod) should:**
  1. Fix the `scanner.py` venv-pruning gap first (add `venv`/`venvs` — and
     confirm against the other known-missing trees from
     [[backup-project-shape]]: `.cache` won't match `"cache"` due to the
     leading dot, `weights`, `comfy/models_shared`, `.pod-persist/*`).
  2. Then follow `docs/DEPLOY.md` from step 0 — it's written exactly for this
     handoff (verify install came across → **read-only** discovery of the
     CPU pod's existing startup mechanism, do not skip this or overwrite an
     unknown Container Start Command → wire in the supervisor → bring it up →
     re-run `dry-run` clean → prove restart survival). Steps 0's three items
     (S3 credentials, healthchecks.io URL, capturing the CPU pod's current
     Container Start Command) are still open — see §6 below, unchanged.
  3. Daniel's four-gate sequence (§ below) is still fully open — dry-run
     completing is gate zero, nothing past it has been started.
- PID 120467 was left running rather than killed — it's read-only and
  harmless, and dies with the pod regardless.

---

## -1. Checkpoint 2026-08-16

- **`config/policy.yaml` open items resolved by Daniel:** `table_read` removed
  from `critical` (now deliberately unmapped → UNCLASSIFIED, will surface in
  dry-run, matching the "never silently discard" rule). `pipeline` added to
  `redownloadable` (legacy per root CLAUDE.md, superseded by `erf/`).
  `POC_VFX` three-way split approved as-is, no change. Still open, unchanged
  from before: `sqlite_databases: []` empty (Daniel to confirm live vs.
  archived `.db` files before seed), and the small unmapped roots (`catalog`,
  `services`, `quarantine`, `image_folder_scan`, `erf_data_backup`) — will
  surface in dry-run, no decision needed yet.
- **`retention.py`'s GC fix (from 2026-08-15) is now functionally verified**
  against the real 40-day-admit/5-day-reject case, run from `.venv/bin/python`:
  40-day orphan admitted, 5-day orphan rejected, still-referenced blob never
  touched, exact-30-day boundary correctly admitted. Probe was written fresh
  and deleted after use (not kept as a permanent test — see next point).
- **`_gen/tests.out` (the generated 41-test suite) is NOT usable as-is and was
  NOT extracted into `tests/`.** It imports zero `app.*` modules — it defines
  its own fake stand-ins (`PolicyEngine`, `BlobStore`, `CapacityManager`,
  `FakeS3`, `BackupEngine`, etc.) and tests those instead of the real code.
  The file's own trailing notes say so: "the exact method names... are
  assumptions... replace them with the real names when integrating." It's
  also structurally malformed (a dangling `in (2, 3)` fragment sits outside
  any code fence around line 820 — looks like two generation passes got
  concatenated badly). Extracting it would produce 41 tests that pass while
  testing nothing real. Someone should eventually rewrite these against the
  real `app.*` API (or throw them out and write new integration tests), but
  it's not a mechanical extraction job. `tests/test_policy_regression.py`
  remains the only real test file.
- **Found and fixed a third integration bug of the same shape as the two from
  2026-08-15** (DeepSeek inventing its own schema instead of using the
  established one) — this one architectural, not a field-name typo:
  **`app/runner.py`'s `Runner` class never used `app/statedb.py`'s `StateDB`
  at all.** It opened its own raw `sqlite3.connect(state_db_path)` and ran its
  own `executescript()` migration against a completely private schema
  (`transactions`, `scan_items`, `known_hashes`, and — critically — its own
  table **also named `blobs`**, with different columns: `sha256/local_path/
  remote_checked/remote_exists` vs. StateDB's `hash/reference_count/
  unreferenced_since`). Both point at the same physical `state.sqlite`. Since
  both use `CREATE TABLE IF NOT EXISTS`, whichever object's migration ran
  first would silently win the table shape and the other would throw
  `sqlite3.OperationalError: no such column` the first time both actually ran
  against the same file. Separately, because Runner never wrote into
  `StateDB.blobs`, `retention.py`'s GC (itself correct, see above) had no real
  data to ever act on — it would have run forever against an empty table.
  - **Full unification (Runner's ~500 lines of scan/upload/verify SQL fully
    rewritten onto StateDB's API) was scoped as future work, not attempted —
    it would require extending StateDB with capability it doesn't have yet
    (per-file local-cache path, remote-HEAD-checked flag, per-transaction
    upload/verify status for resume). That's a rearchitecture, not a patch.**
  - **What was actually fixed (scoped, Daniel-approved):** (1) renamed
    Runner's private `blobs` table to `runner_blobs` everywhere it's
    referenced (`_init_schema`, `_get_local_blob`, `_upsert_blob`) — removes
    the collision/crash. (2) `Runner.__init__` now also opens
    `self.canonical_state = StateDB(self.state_db_path)` alongside its own
    raw connection — safe, since after (1) the two connections touch disjoint
    tables. (3) `_upload_manifest` now calls `canonical_state.record_blob(...)`
    + `canonical_state.bump_refcount(sha, delta=1)` for every blob in the
    manifest being committed, wrapped in try/except so bookkeeping can never
    fail the actual transaction.
  - Patch was drafted via `ds_flash.sh patch_draft` (required by the repo's
    edit-size hook for files >200 lines), diffed against the original before
    applying — diff was exactly the 4 intended changes, nothing else touched
    (933→957 lines, matches the added import + init line + try/except block).
  - **Functionally verified** with a throwaway probe run from
    `.venv/bin/python`: `runner_blobs` still works standalone after the
    rename; both `blobs` and `runner_blobs` coexist in one `state.sqlite`
    without collision; committing a manifest referencing a blob populates
    `StateDB.blobs` with `reference_count=1`; a second manifest referencing
    the same blob correctly bumps it to `2`. Probe deleted after use.
- **New gap found, not yet fixed:** `bin/backupctl` calls
  `app.runner.dry_run(...)`, `app.runner.run_today()`, and
  `app.runner.retry_date(date)` as **module-level functions** — none of these
  exist in `runner.py` (only the `Runner` class and its methods do). This is
  part of the still-not-started "wire `runner` ↔ `backupd`" work, not
  something invented today — flagging so it's not mistaken for already done.
  Also confirms `app/statedb.py` had **zero importers anywhere in the
  codebase** before today's `runner.py` change — it was fully wired up but
  never actually connected to anything.
- **`app/storage.py` was missing entirely — extracted.** `_gen/storage.out`
  (the real boto3 S3 wrapper, `RunPodStorage`) generated successfully on
  2026-08-15 but was never extracted into `app/`, unlike the other 11
  modules. Extracted, syntax-checked, `boto3` import verified in `.venv`.
  Its API (`put_object_from_file/head_object/get_object_to_file/...`,
  `head_object` returning a bare `int | None`) does **not** match what
  `runner.py`'s `Config.blob_store` expects (`put_file/head_object` returning
  an object with `.size`/`download_to_path`) — a fourth interface mismatch of
  the same shape, **not fixed**, out of scope for this session (`run`/`retry`
  are stubbed out below specifically because of this).
- **`bin/backupctl` fully rewritten to call real modules — Daniel's chosen
  direction over building the fictional glue layer.** Every subcommand now
  calls directly into `app.policy.Policy`, `app.scanner.scan`,
  `app.statedb.StateDB`, `app.storage.RunPodStorage`, `app.healthcheck`,
  `app.restore`. No new modules invented; `_call()`/`importlib` shim removed
  entirely (confirmed zero remaining references to any of the 10 fictional
  modules). Config/state-path/source-root/bucket all read directly from
  `config/policy.yaml` via a handful of small local helpers
  (`_load_policy_yaml`, `_open_state_db`, `_build_storage`,
  `_next_melbourne_run`, `_last_success_run_date`) — deliberately not a new
  `app.config` module, per the "call real modules directly" direction.
  - **Real and functionally verified against the live repo** (not a probe —
    actual `backupctl <cmd>` invocations): `status` (correct real numbers:
    93.1 GiB capacity from policy.yaml, 0 B used from a fresh `state.sqlite`,
    correct next-Melbourne-midnight), `doctor` (all 10 checks run for real —
    6 PASS, 4 FAIL, and **every FAIL is the documented, expected blocker**:
    no S3 credentials, daemon not deployed, no successful snapshot yet — not
    a crash), `inventory`/`dry-run` (classification pass verified against
    real `scan`+`Policy.classify` on a small real subtree — report writes
    valid JSON via temp-file-then-replace; **not yet run against the full
    2.8 TB/2.3M-file source root** — see next action below), `snapshots`/
    `restore`/`rebuild-state` (real S3 calls, fail cleanly with the genuine
    `MissingCredentials` error, not a fictional-module crash), `report`
    (finds latest `reports/*.json` by mtime).
  - **`run`/`retry`/`verify` are deliberately left as clear stubs**, not
    wired: `run`/`retry` because `Runner` needs real `capacity`/`retention`/
    `gc` adapter objects that were never designed to match `retention.py`'s
    actual functions, plus the `blob_store` interface mismatch above;
    `verify` because `runner.py` has no standalone verify entry point outside
    a live transaction's phase machine. Each prints exactly what's missing
    and points here instead of pretending to work.
- **Full `backupctl dry-run` against the real 2.8 TB/2.3M-file source root
  launched 2026-08-16 ~10:25 UTC, backgrounded** (Daniel approved running it
  this session). Command: `BACKUP_HOME=/workspace/_BACKUP .venv/bin/python
  bin/backupctl dry-run` from `/workspace/_BACKUP`, stdout+stderr redirected
  to `logs/dry-run-manual.log`. JSON report lands under
  `reports/dry-run-<timestamp>.json` on completion (temp-file-then-replace,
  so a partial write is never visible as a finished report). **As of the
  last check this session it was still running** (`ps aux` showed PID 120467
  alive, no output in the log yet, nothing new under `reports/` yet — normal
  for a 2.3M-file traversal on MooseFS this early). **On resume: check `ps
  aux | grep backupctl` and `tail logs/dry-run-manual.log` /
  `ls -la reports/` before doing anything else — if it's still running, wait
  for it (do not launch a second one on top of it); if it finished, read the
  newest `reports/dry-run-*.json` and build the structured report Daniel
  asked for (format spec below) before touching any of the four gates.** The
  background process was started via the harness's own backgrounding
  mechanism (not a raw `nohup … &`, which is the documented-unsafe pattern
  from 2026-08-15 — see traps below), so it should survive a chat
  clear/compaction; the pod itself is what actually needs to stay up.
- **`scanner.py`'s pruning is name-based only, not policy-integrated —
  confirmed while the dry-run ran.** `DEFAULT_REDOWNLOADABLE_DIR_NAMES =
  {camera_rushes, camera-rushes, redownloadable, downloads, cache}` (plus
  `.backup-always`/`.backup-never` markers) is the entire pruning mechanism.
  None of it matches this project's real excluded trees (`footage` 1.2TB
  PRINCIPAL, `.cache` — leading dot, doesn't match `"cache"` — `venvs`,
  `weights`, `comfy/models_shared`, `.pod-persist/*`). There is no call from
  `scanner.py` into `policy.py`'s real path rules at all. So this dry-run (and
  any nightly run using the scanner as-is) does a **full unpruned traversal**
  of all 2.3M files, including the excluded footage, before per-file
  classification happens. Fine for a one-off dry-run; not acceptable as
  nightly behaviour — matches HANDOFF's original open question from
  2026-08-15 ("does scanner prune by policy or only a hardcoded root list") —
  now answered: hardcoded list only. **Building real policy-directed
  pre-descent pruning is required before nightly runs are viable**, separate
  from the four gates below.

## Daniel's gate sequence, set 2026-08-16 (do not skip ahead of this)

Confirmed order: **dry-run completes → Daniel audits classification/capacity
numbers → wire `run`/`retry`/`verify` for real → real-code integration tests
→ S3 functional tests against the live bucket → seed.** Not
dry-run-then-seed. Four hard gates before the first seed upload, all still
open:

1. **Replace the fake 41-test suite with real tests against the actual
   modules** — not fewer tests, different (real) ones. Must directly exercise
   `runner → StateDB → snapshot commit → snapshot_entries/blob refcounts →
   retention/GC`. Specific scenario Daniel wants proven: commit snapshot
   {A,B} → refcounts increment; commit second snapshot {A,C} → A=2,B=1,C=1;
   expire first snapshot → A=1,B=0,C=1, B gets `unreferenced_since` but isn't
   immediately deleted; rebuild state from remote manifests → same logical
   references recovered. Manifest commit + refcount accounting should be one
   coherent StateDB operation where possible — a crash must not leave half
   the snapshot entries/refcounts updated. Local reference counts must always
   be rebuildable from remote manifests, never trusted as unrecoverable
   truth.
2. **Wire `run`, `retry`, `verify` for real** (currently honest stubs —
   see checkpoint above for exactly why: `run`/`retry` need real
   `capacity`/`retention`/`gc` adapters plus the `blob_store` interface
   mismatch fixed; `verify` needs a standalone entry point outside the live
   transaction phase machine). `run`+`verify` must be real before any actual
   backup data goes in. `retry` must be real before autonomous/unattended
   deployment — crash recovery is part of the design, not optional.
3. **Real S3 functional test against the actual `v6a4zjiytb` volume** using
   the real credentials already stored at `config/credentials.env` (per
   Daniel, "no reason to defer this any further"). `storage.py` currently
   only reads `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from the
   environment, **not** from `config/credentials.env` — that wiring is still
   TODO (flagged 2026-08-15, still true). Required exercise, against a
   disposable test prefix: small PutObject → HeadObject (expected size) →
   GetObject → hash comparison → DeleteObject; a multipart upload → HeadObject
   → download → hash → DeleteObject; and a retry/failure path.
4. **Verify the runner.py blob-table fix didn't leave two conceptual blob
   authorities.** The 2026-08-16 fix renamed the colliding table to
   `runner_blobs` and wired refcounting into `StateDB.blobs` at manifest
   commit — but `runner_blobs` (local-cache path, `remote_checked`/
   `remote_exists` flags, used during upload/verify) still exists alongside
   `StateDB.blobs` (canonical refcount truth). Daniel's standard: runner-local
   transaction/work-progress tables are fine; a **duplicate durable blob
   authority** is not. Needs an explicit check that `runner_blobs` only ever
   holds transient per-run upload-progress state, never anything gc_candidates
   or retention logic should be reading as ground truth.

**Same standard now applies everywhere, not just to the fake test suite**:
exercise real implementation paths, not mocks that just reproduce the
intended behaviour. Applies to `run`, `retry`, `verify`, S3, retention,
restore.

### Report format Daniel wants when the dry-run completes
SELECTED (critical/BTS/git-rescue/outputs), EXCLUDED (principal/
redownloadable/quota-or-size rejects), UNCLASSIFIED (total + substantial path
breakdown), MIXED TREE RESULTS (comfy/erf/POC_VFX/motiongen_tests/
melb_acquire/.pod-persist/weights), CAPACITY (projected unique initial size,
projected occupancy, headroom against 70/80/90% thresholds — note 31.6GB of
BTS alone is a meaningful chunk of the 100GB budget), SCANNER (dirs/files
actually visited this run, trees pruned, real vs. hardcoded pruning gap
above, elapsed time), GIT (remote-backed-clean / rescue-required / ambiguous
counts), DATABASES (live DBs identified, snapshot method).
- **That gap is much bigger than three functions — surveyed fully, not
  fixed.** `bin/backupctl` calls `_call("app.<module>", "<function>")` for
  every subcommand. Full survey of every `_call` target: it references 10
  modules that **do not exist anywhere in `app/`** —  `app.state`,
  `app.scheduler`, `app.repository`, `app.inventory`, `app.reports`,
  `app.snapshots` (note: plural — the real file is `snapshot.py`, singular),
  `app.verify`, `app.config`, `app.s3` — plus missing functions on modules
  that *do* exist (`app.runner.dry_run/run_today/retry_date`,
  `app.policy.validate_policy`, `app.healthcheck.validate_healthcheck`). Only
  `app/{backupd,git_rescue,healthcheck,policy,restore,retention,runner,
  scanner,snapshot,statedb,util}.py` are real. `backupctl` was generated
  against a completely different, imagined module layout — this is the same
  "invented its own interface" failure mode as the other three bugs found
  this week, just at CLI-backend scale rather than a function or a schema.
  **Every `backupctl` subcommand is currently non-functional** — this is not
  a small glue-code gap, it's most of a design decision (build a real
  `app.state`/`app.scheduler`/`app.config`/etc. layer to match what
  `backupctl` expects, vs. rewrite `backupctl` to call the 11 real modules
  directly) plus the implementation to match. Stopped here deliberately
  rather than improvising that decision — see next session.

---

## 0. Checkpoint 2026-08-15 (mid-session clear)

- **Install root confirmed:** `/workspace/_BACKUP` is the real, only implementation.
  `/workspace/_system/backup/` (referenced in a later user brief) does not exist
  on disk — do not create a second copy there; if the path matters, that's a
  rename of `_BACKUP`, not a fresh build.
- **S3 credentials now stored** at `config/credentials.env` (plaintext, per
  explicit user authorisation — "security is not a concern for this project").
  Added `_BACKUP/config/credentials.env` to `policy.yaml` `hard_exclude` so it
  is never itself uploaded into a snapshot (consistency, not a security ask).
  **`storage.py` has not yet been edited to source this file** — still TODO.
- **All 16 modules have now generated successfully** (`_gen/_status` and
  `_status2` both show `ALL_DONE`/`DRIVER2_DONE`). Extracted to `app/` and
  `bin/`. Import-checked, not all functionally verified yet — see below.
- **Two real integration bugs found and fixed this session** (both in
  DeepSeek-generated code that invented its own schema instead of matching
  the already-established one):
  1. `app/policy.py` — path-rule branch marked **every** matched class
     `selected=True`, including `PRINCIPAL` and `REDOWNLOADABLE`. Would have
     flagged 1.2 TB of camera-original footage for backup. Fixed via
     `patch_draft`; **verified** — 7/7 pytest regression cases pass
     (`tests/test_policy_regression.py`, includes the RED/BTS path-vs-extension
     case from the brief). Also fixed: `policy.py` was reading a self-invented
     YAML schema (`system_exclusions`/`path_rules`/`output_budget`) instead of
     the real `config/policy.yaml` schema (`hard_exclude`/`paths`/`outputs`).
  2. `app/retention.py` — `gc_candidates()` read blob fields `refcount`/`id`
     where `statedb.py`'s real schema uses `reference_count`/`hash`, and
     required `unreferenced_since` to already be a `datetime` (SQLite returns
     ISO strings). **Fixed via direct Edit, re-imported OK, NOT yet
     re-exercised against the 40-day-admit/5-day-reject GC case** (was doing
     this when interrupted) — redo that check first in the next session.
     Also note: `statedb.unreferenced_blobs()` already implements the same
     grace-window filter correctly (verified earlier) — the real GC pipeline
     in `runner.py` should probably call that directly rather than
     `retention.gc_candidates()`, which is now more of a pure/testable helper.
- **Not yet functionally verified this session** (imported cleanly, nothing
  more): `snapshot.py`, `restore.py`, `scanner.py`, `runner.py`, `storage.py`,
  `bin/backupctl`, `bin/install-startup.sh`, `bin/supervisor.sh`, and the
  freshly-regenerated `tests.out` (39 KB / ~1067 lines, not yet extracted to
  `tests/` or run).
- **Given §6 of the new brief (prune-before-descent scanner requirement)**:
  `scanner.py` was generated from a prompt written *before* that requirement
  was stated this explicitly. It needs a specific check: does it call into
  `policy.py` to classify a *directory* and prune descent for confidently
  PRINCIPAL/REDOWNLOADABLE trees, or does it only prune a hardcoded root list?
  Verify this before trusting nightly traversal cost.
- **`backupd.py` rework (JSON state → statedb.py) — not started.**

## 1. State of play

### Done and functionally verified

| File | Lines | Verified by |
|---|---|---|
| `app/util.py` | ~400 | Real probe: DST offsets flip correctly across **both** Melbourne transitions (+11:00→+10:00); `BACKUP_HEALTHCHECK_URL` and `GITHUB_PAT` detected as secrets while `PATH`/`HOME`/`RUNPOD_POD_ID` are not. |
| `app/statedb.py` | ~460 | Real DB created; schema matches spec exactly; `foreign_keys=1`, WAL; 30-day GC grace correctly admits a 40-day orphan and rejects a 5-day one; phases uppercase `FAILED`/`SUCCESS`. |
| `app/healthcheck.py` | ~323 | No-op when URL unset; on unreachable host logs with the secret **masked** (`http://…/***/start`) and does **not** raise. |
| `app/git_rescue.py` | ~683 | **9/9 classification fixtures pass** (see §3). Read-only guard blocks 21 mutating subcommands; `fetch` refused; no `FETCH_HEAD` created; dirty fixture unmodified. |
| `app/backupd.py` | ~608 | Imports; **needs rework** — see §4. |
| `config/policy.yaml` | — | Structure complete. **`paths:` arrays still empty** — see §4. |
| `README.md`, `docs/DEPLOY.md` | — | Written. |
| `requirements.lock.txt` + `restore-venv.sh` | — | Pinned; restore script probes **by function**, not by import-existence. |

Venv interpreter resolves to `/workspace/.pod-persist/uv-python/...`, **not**
`/usr/bin` — the T-INFRA-05 ephemeral-interpreter trap is avoided.

### Generated but not yet reviewed/installed
`policy`, `scanner`, `snapshot`, `storage`, `retention`, `restore`, `runner`,
`backupctl`, `supervisor.sh`, `install-startup.sh`, `tests` — codegen was still
running at pause. Check `_gen/_status` and `_gen/_status2`, then extract.

### Not started
Wiring `runner` ↔ `backupd`, the dry-run report renderer, the S3 test-prefix
drill, the restore drill.

---

## 2. Source inventory (measured, not estimated)

Full scan: **2,800 GB, 2,304,001 files, 86 top-level entries.**
Raw data: `reports/toplevel_sizes_partial.tsv`, sorted in
`reports/toplevel_sizes_sorted.txt`.

| GB | Files | Path | Indicated class |
|---:|---:|---|---|
| 1231.45 | 1,727 | `footage` | **PRINCIPAL — exclude** |
| 980.61 | 500,880 | `comfy` | mixed; needs sub-rules |
| 116.20 | 675,964 | `erf` | active project; mixed |
| 82.96 | 47,375 | `.cache` | redownloadable |
| 64.82 | 499 | `weights` | redownloadable |
| 56.59 | 277,977 | `motiongen_tests` | outputs |
| 52.54 | 3,532 | `POC_VFX` | **must be split** |
| 43.59 | 23,593 | `melb_acquire` | project + data |
| 37.01 | 105,103 | `.pod-persist` | tooling; mixed |
| 35.70 | 261,778 | `venvs` | redownloadable |
| 31.60 | 1,104 | `BTS` | **BTS — include** |
| 23.29 | 308 | `table_read` | undetermined |
| 11.62 | 202,213 | `pipeline` | legacy code |

`footage` is unambiguous principal photography: 407 RED `.r3d` files totalling
**994 GB**, laid out `DAY_1/2/3 → RUSHES → A CAM / B CAM` with RED naming
(`A003_B016_0924RA_001.R3D`), plus DJI drone and 118 GB of Insta360 `.insv`.

`BTS` is 31.6 GB of `Daniel_BTS` / `Darby_BTS` / `Noah_BTS` phone footage — and
it contains at least one RED-named file (`A001_02051406_C026.mov`). **This is
the concrete proof that extension-based classification would be wrong**, and
why `principal` must be matched by explicit path only.

---

## 3. Git fixtures — keep these

Fixtures live at `…/scratchpad/gt/` and must be rebuilt as a pytest fixture.
All nine currently pass:

| Fixture | Expected |
|---|---|
| `clean` (pushed, upstream set) | `REMOTE_BACKED_CLEAN` |
| `ignoredonly` (only ignored untracked) | `REMOTE_BACKED_CLEAN` |
| `dirty`, `staged`, `untracked`, `ahead` | `RESCUE_REQUIRED` |
| `noupstream`, `noremote`, `detached` | `RESCUE_REQUIRED` |

---

## 4. Next actions, in order

1. **`config/policy.yaml` `paths:` is now filled** (33 critical / 1 bts / 9
   principal / 33 redownloadable / 24 outputs entries), derived from both
   Haiku profiling passes. Validated with `yaml.safe_load` — parses clean.
   **Before seeding, resolve these open items Daniel should weigh in on:**
   - `table_read` (23.3 GB, 308 files) — no classification signal was found;
     defaulted to `critical` (never silently discard) but this is a guess.
   - `pipeline` (11.6 GB) — CLAUDE.md marks it explicitly **legacy**, so it may
     belong under `redownloadable`/excluded rather than `critical`. Left
     **unmapped → UNCLASSIFIED** deliberately rather than guessing; it will
     show up in the dry-run report for a decision.
   - Also unmapped, all small (`catalog` 0.3 GB, `services` 0.24 GB,
     `quarantine` 0.07 GB, `image_folder_scan` 0.06 GB, `erf_data_backup`
     5.3 GB) — same treatment, will surface in dry-run.
   - `sqlite_databases: []` is still empty — `.db` files were seen in
     `archive/catalog_20260202T132929Z/catalog_full/` and `catalog/` but not
     confirmed live vs. static-archived copies.
   - `POC_VFX` was split (Shots minus plate/houdini_caches → outputs; plate →
     principal-excluded; houdini_caches + RoomScan/OLD → redownloadable) —
     worth Daniel's sanity check before seeding, it's a judgement call.
2. **Extract the remaining generated modules** (`scanner`, `snapshot`,
   `storage`, `restore`, `runner`, `supervisor.sh`, `install-startup.sh`,
   `tests` — check `_gen/_status`/`_status2` for what landed). Fence-stripping
   one-liner is in the session; validate by **content**, not exit code (§5).
3. **Rework `app/backupd.py`.** DeepSeek gave it a private JSON state file; it
   must use `app/statedb.py` (`get_run`/`set_phase`/`mark_run_*`) and branch on
   the uppercase phase names, so resume/catch-up/hold work off one source of
   truth.
4. **Wire `runner` ↔ `backupd`**, then `backupctl doctor`.
5. **Dry-run, then STOP.** Do not seed. The dry-run report is the one
   deliberate approval point.

---

## 5. Traps hit in this session — do not re-learn these

- **`ds_flash.sh` has an internal `curl --max-time 200s` and exits 0 when it
  trips.** A timed-out codegen looks like success. Always set `DS_MAX_TIME=900`
  and **validate output by content**, never by `$?`. Six modules silently
  produced 0-byte files before this was caught.
- **`nohup … &` inside a Bash tool call gets reaped** when the call returns.
  Use `setsid` plus the harness's own background mechanism.
- **`pkill -f <pattern>` matches the issuing shell** when the pattern appears in
  its own command line (e.g. via a heredoc). It killed this session's shell
  twice. Exclude self, or match on a narrower pattern.
- **`grep -n` output is being mangled** by something in the harness (it reported
  "6 matches in 5 files" for a single-file grep). Use Python for source
  inspection.
- **A hook requires `ds_flash.sh codegen` before writing any >80-line `.py`/`.sh`**,
  and `patch_draft` before reading/editing any >200-line source file. Obey them;
  they name the required command.
- `du -sh /workspace` **times out** — it is MooseFS, not local disk. Scan one
  tree at a time with per-tree `timeout`.

---

## 6. Blockers requiring Daniel (historical, 2026-08-15 — see checkpoint -3d at top for current status)

1. ~~**RunPod S3 credentials do not exist anywhere on this volume.**~~ **STALE as of 2026-08-17** —
   credentials were supplied later the same day (checkpoint 0 below) and stored at
   `config/credentials.env`; `app/storage.py` now loads them automatically (checkpoint -3d). The
   *current* open item is different and narrower: the stored key pair fails real S3 auth
   (`SignatureDoesNotMatch`) — needs Daniel to confirm/re-supply from RunPod console → Settings →
   **S3 API Keys**, not a "does it exist" question anymore.
2. ~~**Healthchecks.io check**~~ **NOT a blocker** as of 2026-08-17 (Daniel: only needed before
   production monitoring, not for dry-run/S3-testing/implementation). Still true when it's time to
   set it up: create in the UI, then `BACKUP_HEALTHCHECK_URL` as a pod env var, schedule
   `0 0 * * *`, timezone `Australia/Melbourne`, grace 8h.
3. **CPU pod's current Container Start Command** — resolved in checkpoint -3b (it was empty/null;
   nothing needed preserving). Kept here for history only.

Credential *rotation/reset/generation* is Daniel's action alone, always — routine *use* of
already-supplied credentials is not gated (see global CLAUDE.md, revised 2026-08-17).
