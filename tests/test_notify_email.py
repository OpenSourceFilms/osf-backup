"""Regression tests for app.notify_email.EmailClient -- the daily backup
report SMTP sender. Mocks smtplib.SMTP so no real network/credentials are
needed; monkeypatches env vars and CREDENTIALS_ENV_PATH so each test is
isolated from both the real environment and the real config/credentials.env
file. Never allowed to actually attempt a network connection.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import notify_email as app_notify_email

_ENV_VARS = (
    "BACKUP_EMAIL_ENABLED",
    "BACKUP_EMAIL_TO",
    "BACKUP_SMTP_HOST",
    "BACKUP_SMTP_PORT",
    "BACKUP_SMTP_USERNAME",
    "BACKUP_SMTP_PASSWORD",
    "BACKUP_EMAIL_FROM",
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test starts from a clean slate: no env vars, and
    CREDENTIALS_ENV_PATH points at a file that doesn't exist yet -- real
    config/credentials.env must never leak into these tests."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        app_notify_email, "CREDENTIALS_ENV_PATH", tmp_path / "credentials.env"
    )


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("BACKUP_EMAIL_ENABLED", "1")
    defaults = {
        "BACKUP_EMAIL_TO": "ops@example.com",
        "BACKUP_SMTP_HOST": "smtp.example.com",
        "BACKUP_SMTP_USERNAME": "user@example.com",
        "BACKUP_SMTP_PASSWORD": "app-password",
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setenv(name, value)


def test_disabled_by_default_is_a_documented_noop():
    client = app_notify_email.EmailClient()
    result = client.send("subject", "body")
    assert result.status == "disabled"
    assert result.attempts == 0


def test_enabled_but_missing_required_config_is_misconfigured(monkeypatch):
    monkeypatch.setenv("BACKUP_EMAIL_ENABLED", "true")
    # deliberately leave BACKUP_EMAIL_TO/SMTP_* unset
    client = app_notify_email.EmailClient()
    result = client.send("subject", "body")
    assert result.status == "misconfigured"
    assert "BACKUP_EMAIL_TO" in result.error
    assert "BACKUP_SMTP_HOST" in result.error


def test_successful_send_uses_starttls_and_login(monkeypatch):
    _enable(monkeypatch)
    client = app_notify_email.EmailClient()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__.return_value = mock_smtp_instance
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)

    with patch("app.notify_email.smtplib.SMTP", mock_smtp_cls):
        result = client.send("Test Subject", "Test body")

    assert result.status == "sent"
    assert result.attempts == 1
    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    mock_smtp_instance.starttls.assert_called_once()
    mock_smtp_instance.login.assert_called_once_with("user@example.com", "app-password")
    assert mock_smtp_instance.send_message.call_count == 1
    sent_message = mock_smtp_instance.send_message.call_args[0][0]
    assert sent_message["Subject"] == "Test Subject"
    assert sent_message["To"] == "ops@example.com"


def test_failed_send_retries_up_to_max_attempts_then_reports_failed(monkeypatch):
    _enable(monkeypatch)
    client = app_notify_email.EmailClient(max_attempts=3, retry_backoff_seconds=0)

    mock_smtp_cls = MagicMock(side_effect=OSError("connection refused"))

    with patch("app.notify_email.smtplib.SMTP", mock_smtp_cls), \
         patch("app.notify_email.time.sleep") as mock_sleep:
        result = client.send("subject", "body")

    assert result.status == "failed"
    assert result.attempts == 3
    assert "connection refused" in result.error
    assert mock_smtp_cls.call_count == 3
    assert mock_sleep.call_count == 2  # backoff between attempts, not after the last


def test_from_addr_defaults_to_smtp_username(monkeypatch):
    _enable(monkeypatch)
    client = app_notify_email.EmailClient()
    assert client.from_addr == "user@example.com"


def test_from_addr_override_respected(monkeypatch):
    _enable(monkeypatch, BACKUP_EMAIL_FROM="reports@example.com")
    client = app_notify_email.EmailClient()
    assert client.from_addr == "reports@example.com"


def test_credentials_env_file_fallback_used_when_env_unset(tmp_path, monkeypatch):
    creds_path = tmp_path / "credentials.env"
    creds_path.write_text(
        "BACKUP_EMAIL_ENABLED=1\n"
        "BACKUP_EMAIL_TO=ops@example.com\n"
        "BACKUP_SMTP_HOST=smtp.example.com\n"
        "BACKUP_SMTP_USERNAME=user@example.com\n"
        "BACKUP_SMTP_PASSWORD=app-password\n"
    )
    monkeypatch.setattr(app_notify_email, "CREDENTIALS_ENV_PATH", creds_path)

    client = app_notify_email.EmailClient()
    assert client.enabled is True
    assert client.to_addr == "ops@example.com"


def test_env_var_takes_precedence_over_credentials_file(tmp_path, monkeypatch):
    creds_path = tmp_path / "credentials.env"
    creds_path.write_text("BACKUP_SMTP_HOST=from-file.example.com\n")
    monkeypatch.setattr(app_notify_email, "CREDENTIALS_ENV_PATH", creds_path)
    monkeypatch.setenv("BACKUP_SMTP_HOST", "from-env.example.com")

    client = app_notify_email.EmailClient()
    assert client.smtp_host == "from-env.example.com"


def test_format_success_email_subject_and_body_shape():
    report = {
        "txn_id": "backup-2026-08-28",
        "txn_date": "2026-08-28",
        "created_at": "2026-08-28T00:00:00+10:00",
        "updated_at": "2026-08-28T07:02:17+10:00",
        "file_count": 94130,
        "protected_bytes": 194000000000,
        "by_classification": {
            "CRITICAL": {"count": 100, "bytes": 5000},
            "OUTPUTS": {"count": 200, "bytes": 10000},
        },
        "top_roots": [{"root": "erf", "count": 50, "bytes": 8000}],
        "deferred_active": {"count": 0, "paths": [], "truncated": False},
        "diff_from_previous_success": None,
        "phase": "SUCCESS",
    }

    subject, body = app_notify_email.format_success_email(report)

    assert subject.startswith("[OSF Backup OK] 2026-08-28")
    assert "94,130" in subject
    assert "backup-2026-08-28" in body
    assert "OUTPUTS: 200 files" in body
    assert "erf: 50 files" in body
    assert "Diff from previous SUCCESS: none" in body
    assert "Result: SUCCESS" in body


def _success_report(**overrides):
    report = {
        "txn_id": "backup-2026-09-08",
        "txn_date": "2026-09-08",
        "created_at": "2026-09-08T04:00:00+10:00",
        "updated_at": "2026-09-08T09:03:00+10:00",
        "file_count": 67678,
        "protected_bytes": 185615689331,
        "by_classification": {"CRITICAL": {"count": 100, "bytes": 5000}},
        "top_roots": [
            {"root": f"root{i}", "count": i, "bytes": 1000 - i} for i in range(15)
        ],
        "deferred_active": {"count": 0, "paths": [], "truncated": False},
        "diff_from_previous_success": None,
        "phase": "SUCCESS",
    }
    report.update(overrides)
    return report


def test_daily_report_states_what_was_NOT_backed_up():
    """The point of the daily email. A report listing only successes gives
    the reader no way to notice a policy gap -- the 2026-09-07 run
    protected 67,678 files and skipped 46,748 as UNCLASSIFIED, and until
    this landed that number appeared nowhere a human would see it."""
    body = app_notify_email.format_success_email(
        _success_report(
            scan_skipped={
                "REDOWNLOADABLE": 590110,
                "UNCLASSIFIED": 46748,
                "PRINCIPAL": 1744,
            }
        )
    )[1]

    assert "Not backed up: 638,602 files skipped by policy" in body
    assert "UNCLASSIFIED: 46,748" in body
    assert "config/policy.yaml" in body
    # ordered by count, so the biggest category leads
    assert body.index("REDOWNLOADABLE: 590,110") < body.index("PRINCIPAL: 1,744")


def test_unmeasured_skips_are_never_reported_as_zero():
    """A transaction predating the accounting must say so. Rendering it as
    "nothing skipped" would be the report asserting a fact it does not
    have -- exactly the absence-as-signal failure this system forbids."""
    body = app_notify_email.format_success_email(_success_report(scan_skipped=None))[1]
    assert "Not backed up: not measured for this transaction" in body


def test_nothing_skipped_is_distinct_from_not_measured():
    body = app_notify_email.format_success_email(_success_report(scan_skipped={}))[1]
    assert "every scanned file was selected" in body


def test_report_stays_brief_enough_to_actually_read():
    """At 20 path samples per diff category this ran to 102 lines, most of
    it path lists. A daily email nobody finishes is a daily email nobody
    reads. Counts stay exact; only the illustrative samples are trimmed."""
    diff = {
        "previous_txn_id": "backup-2026-09-07",
        "added_count": 4012,
        "added_paths": [f"/workspace/a/{i}" for i in range(20)],
        "changed_count": 1043,
        "changed_paths": [f"/workspace/c/{i}" for i in range(20)],
        "disappeared_count": 30464,
        "disappeared_paths": [f"/workspace/d/{i}" for i in range(20)],
        "new_unique_bytes": 23806118298,
    }
    body = app_notify_email.format_success_email(
        _success_report(diff_from_previous_success=diff, scan_skipped={})
    )[1]

    assert len(body.splitlines()) < 70, "daily email has grown back into a wall of text"
    assert "added: 4,012" in body
    assert "... and 4,007 more" in body
    assert "... and 30,459 more" in body
    assert body.count("/workspace/a/") == app_notify_email.EMAIL_SAMPLE_PATHS
    assert body.count("root") <= app_notify_email.EMAIL_TOP_ROOTS + 2


def test_deletions_are_flagged_for_a_human_to_sanity_check():
    """30,464 files disappeared on 2026-09-07 and it was legitimate -- but
    an accidental rm looks identical here, and this is the only place it
    surfaces while the older snapshots are still retained."""
    diff = {
        "previous_txn_id": "backup-2026-09-07",
        "added_count": 0, "added_paths": [],
        "changed_count": 0, "changed_paths": [],
        "disappeared_count": 30464,
        "disappeared_paths": ["/workspace/gone.txt"],
        "new_unique_bytes": 0,
    }
    body = app_notify_email.format_success_email(
        _success_report(diff_from_previous_success=diff, scan_skipped={})
    )[1]
    assert "still held in retained snapshots" in body


def test_duration_is_reported():
    body = app_notify_email.format_success_email(_success_report())[1]
    assert "Duration: 5h 3m" in body


def test_duration_never_breaks_the_email_on_bad_timestamps():
    body = app_notify_email.format_success_email(
        _success_report(created_at="not-a-date", updated_at=None)
    )[1]
    assert "Result: SUCCESS" in body
    assert "Duration:" not in body


def test_format_failure_email_subject_and_body_shape():
    report = {
        "txn_id": "backup-2026-08-29",
        "txn_date": "2026-08-29",
        "phase": "SCANNING",
        "resume_phase": None,
        "error": "simulated crash",
        "progress": {"uploaded": 0, "total_scan_items": 42866},
        "last_known_path": "/workspace/erf/foo.txt",
    }

    subject, body = app_notify_email.format_failure_email(report)

    assert subject == "[OSF BACKUP FAILED] 2026-08-29 — SCANNING"
    assert "simulated crash" in body
    assert "0 / 42,866" in body
    assert "/workspace/erf/foo.txt" in body
