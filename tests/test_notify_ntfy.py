"""Unit tests for app.notify_ntfy.NtfyClient and its formatters."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import notify_ntfy as ntfy
from app.healthcheck import NotificationResult


def _write_credentials(tmp_path, body):
    path = tmp_path / "credentials.env"
    path.write_text(body, encoding="utf-8")
    return path


def _no_http_call(*args, **kwargs):
    raise AssertionError("HTTP call should not have been made")


def test_priority_levels_dict_matches_ntfy_documented_scale():
    assert ntfy._PRIORITY_LEVELS == {
        "min": 1,
        "low": 2,
        "default": 3,
        "high": 4,
        "urgent": 5,
        "max": 5,
    }


def test_priority_and_tags_are_set_per_notification_kind(monkeypatch):
    captured = []

    def fake_send(self, title, message, *, priority="default", tags=None):
        captured.append((priority, list(tags) if tags else None))
        return NotificationResult(status="sent", attempts=1)

    monkeypatch.setattr(ntfy.NtfyClient, "send", fake_send)
    client = ntfy.NtfyClient.__new__(ntfy.NtfyClient)

    report = {
        "txn_id": "backup-2026-09-07",
        "txn_date": "2026-09-07",
        "file_count": 1,
        "protected_bytes": 10,
        "error_count": 0,
        "deferred_active": {"count": 0, "paths": []},
        "by_classification": {},
        "scan_skipped": None,
        "local_staging_bytes": None,
        "phase": "SUCCESS",
        "progress": {"uploaded": 1, "total_scan_items": 1},
        "total_selected_bytes": 10,
        "error": None,
        "resume_phase": None,
    }

    client.success(report)
    client.failure(report)
    client.interrupted("backup-2026-09-07", "UPLOADING", "operator restart")
    client.test_notification()

    priorities = [call[0] for call in captured]
    tags = [call[1] for call in captured]

    assert priorities == ["default", "urgent", "high", "default"]
    assert [ntfy._PRIORITY_LEVELS[p] for p in priorities] == [3, 5, 4, 3]
    assert tags == [
        ["white_check_mark", "floppy_disk"],
        ["rotating_light", "warning"],
        ["pause_button"],
        ["test_tube"],
    ]


def test_format_success_notification_includes_top_classifications_deferred_and_unclassified():
    report = {
        "txn_id": "backup-2026-09-07",
        "txn_date": "2026-09-07",
        "created_at": "2026-09-07T00:00:00",
        "updated_at": "2026-09-07T00:05:00",
        "file_count": 10,
        "protected_bytes": 2048,
        "error_count": 1,
        "deferred_active": {"count": 2, "paths": ["a", "b"]},
        "by_classification": {
            "erf": {"count": 5, "bytes": 3000},
            "home": {"count": 3, "bytes": 2000},
            "other": {"count": 2, "bytes": 1000},
        },
        "scan_skipped": {"UNCLASSIFIED": 4, "OTHER": 1},
        "local_staging_bytes": 1000,
    }

    title, message = ntfy.format_success_notification(report)

    assert title == "OSF Backup OK — 2026-09-07"
    # Top classification entries are the compact "top roots" shown on the
    # phone. (`NTFY_SAMPLE_PATHS` exists in the module but current
    # format_success_notification does not include sample paths.)
    assert "erf" in message
    assert "home" in message
    assert "other" in message
    assert "1 upload errors" in message
    assert "2 deferred" in message
    assert "4 unclassified skipped" in message


def test_format_failure_notification_contains_phase_error_and_resume_phase():
    report = {
        "txn_id": "backup-2026-09-07",
        "txn_date": "2026-09-07",
        "phase": "FAILED",
        "progress": {"uploaded": 3, "total_scan_items": 10},
        "total_selected_bytes": 5000,
        "protected_bytes": 1000,
        "error": "simulated upload failure",
        "resume_phase": "UPLOADING",
    }

    title, message = ntfy.format_failure_notification(report)

    assert "OSF BACKUP FAILED" in title
    assert "Phase: FAILED" in message
    assert "3 / 10 files" in message
    assert "Error: simulated upload failure" in message
    assert "Resume phase: UPLOADING" in message


def test_format_interrupted_notification_content():
    title, message = ntfy.format_interrupted_notification(
        "backup-2026-09-07", "UPLOADING", "operator shutdown"
    )

    assert title == "OSF Backup Interrupted — backup-2026-09-07"
    assert "Stopped at UPLOADING, not a failure -- will resume." in message
    assert "operator shutdown" in message


def test_mask_topic_url_does_not_leak_full_topic():
    masked = ntfy._mask_topic_url("https://ntfy.sh/super-secret-topic-1234567890")
    assert "super-secret-topic-1234567890" not in masked
    assert "supe***" in masked


def test_disabled_client_is_silent_noop_and_makes_no_http_call(tmp_path, monkeypatch):
    path = _write_credentials(tmp_path, "BACKUP_NTFY_ENABLED=0\n")
    monkeypatch.setattr(ntfy, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.delenv("BACKUP_NTFY_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_NTFY_URL", raising=False)

    monkeypatch.setattr(ntfy.urllib.request, "urlopen", _no_http_call)

    client = ntfy.NtfyClient()
    result = client.send("title", "message")

    assert result.status == "disabled"
    assert result.attempts == 0


def test_enabled_but_missing_url_is_misconfigured_and_makes_no_http_call(tmp_path, monkeypatch):
    path = _write_credentials(tmp_path, "BACKUP_NTFY_ENABLED=1\n")
    monkeypatch.setattr(ntfy, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.delenv("BACKUP_NTFY_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_NTFY_URL", raising=False)

    monkeypatch.setattr(ntfy.urllib.request, "urlopen", _no_http_call)

    client = ntfy.NtfyClient()
    result = client.send("title", "message")

    assert result.status == "misconfigured"
    assert result.error == "Missing required config: BACKUP_NTFY_URL"
    assert result.attempts == 0


def test_bounded_retry_with_linear_backoff_never_raises(tmp_path, monkeypatch):
    path = _write_credentials(
        tmp_path,
        "BACKUP_NTFY_ENABLED=1\nBACKUP_NTFY_URL=https://ntfy.sh/secret-topic\n",
    )
    monkeypatch.setattr(ntfy, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.delenv("BACKUP_NTFY_ENABLED", raising=False)
    monkeypatch.delenv("BACKUP_NTFY_URL", raising=False)

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise OSError("network down")

    sleeps = []
    monkeypatch.setattr(ntfy.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ntfy.time, "sleep", lambda seconds: sleeps.append(seconds))

    client = ntfy.NtfyClient()
    result = client.send("title", "message")

    assert result.status == "failed"
    assert result.attempts == 3
    assert "OSError" in result.error
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]
