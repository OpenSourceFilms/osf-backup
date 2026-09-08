#!/usr/bin/env python3
"""RTFM UserPromptSubmit hook — keep the background worker alive.

No sync here. The single background worker handles discovery (idle-scan),
ingestion, embeddings and OCR. This hook only revives that worker if it
died, so a new session brings the pipeline back. Cheap by design: no
filesystem scan, no hashing, nothing on the user's hot path.
"""
import os, time
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def _log(msg):
    try:
        ts = time.strftime("%H:%M:%S")
        with open(PROJECT_ROOT / ".rtfm" / "rtfm.log", "a") as f:
            f.write(f"[{ts}]       hook | {msg}\n")
    except Exception:
        pass


def main():
    rtfm_dir = PROJECT_ROOT / ".rtfm"
    if not (rtfm_dir / "library.db").exists():
        return
    try:
        from rtfm.cli_worker import ensure_worker_running
        pid = ensure_worker_running(rtfm_dir)
        if pid:
            _log(f"worker alive pid={pid}")
    except Exception as e:
        _log(f"worker ensure ERROR: {e}")


if __name__ == "__main__":
    main()
