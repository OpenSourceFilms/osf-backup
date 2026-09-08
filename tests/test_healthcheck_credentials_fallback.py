"""The dead-man URL must be readable from where the operator is told to put it.

config/credentials.env ships a commented ``BACKUP_HEALTHCHECKS_URL=`` line,
and app/storage.py and app/notify_email.py both fall back to that file for
their own secrets. HealthcheckClient did not: until 2026-09-07 it read
os.environ alone, so pasting the URL exactly where the file invited it
would have configured nothing at all. The only symptom would be a dead-man
switch that silently never armed -- the precise failure a dead-man exists
to catch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import healthcheck as hc


def _write_credentials(tmp_path, body):
    path = tmp_path / "credentials.env"
    path.write_text(body, encoding="utf-8")
    return path


def test_url_is_read_from_the_credentials_file(tmp_path, monkeypatch):
    path = _write_credentials(
        tmp_path,
        "# leading comment\n"
        "AWS_ACCESS_KEY_ID=irrelevant\n"
        "BACKUP_HEALTHCHECKS_URL=https://hc-ping.example/aaaa-bbbb\n",
    )
    monkeypatch.setattr(hc, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.delenv("BACKUP_HEALTHCHECKS_URL", raising=False)

    assert hc.HealthcheckClient().url == "https://hc-ping.example/aaaa-bbbb"


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    """The file is a fallback, not an override -- an operator exporting a
    different URL for a one-off run must not be silently overruled."""
    path = _write_credentials(
        tmp_path, "BACKUP_HEALTHCHECKS_URL=https://hc-ping.example/from-file\n"
    )
    monkeypatch.setattr(hc, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.setenv("BACKUP_HEALTHCHECKS_URL", "https://hc-ping.example/from-env")

    assert hc.HealthcheckClient().url == "https://hc-ping.example/from-env"


def test_commented_out_line_is_not_treated_as_configured(tmp_path, monkeypatch):
    """The shipped file has the key commented out. Reading it as a value
    would produce a client that pings a nonsense URL forever."""
    path = _write_credentials(tmp_path, "# BACKUP_HEALTHCHECKS_URL=\n")
    monkeypatch.setattr(hc, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.delenv("BACKUP_HEALTHCHECKS_URL", raising=False)

    assert hc.HealthcheckClient().url is None


def test_missing_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "CREDENTIALS_ENV_PATH", tmp_path / "nope.env")
    monkeypatch.delenv("BACKUP_HEALTHCHECKS_URL", raising=False)

    assert hc.HealthcheckClient().url is None


def test_configured_url_is_never_logged_in_full(tmp_path, monkeypatch):
    """The Healthchecks UUID is the entire credential."""
    path = _write_credentials(
        tmp_path, "BACKUP_HEALTHCHECKS_URL=https://hc-ping.example/secret-uuid\n"
    )
    monkeypatch.setattr(hc, "CREDENTIALS_ENV_PATH", path)
    monkeypatch.delenv("BACKUP_HEALTHCHECKS_URL", raising=False)

    client = hc.HealthcheckClient()
    assert "secret-uuid" not in client._mask_url(client.url)
