#!/bin/bash
# One-command venv restore. The interpreter lives on /workspace (persistent);
# a base-image change or pod rebuild can still drop it, so this must work
# from nothing but this repo.
set -o pipefail
cd "$(dirname "$0")" || exit 1
PY=/workspace/.pod-persist/uv-python/cpython-3.13.8-linux-x86_64-gnu/bin/python3
[ -x "$PY" ] || { echo "FATAL: persistent interpreter missing: $PY"; exit 1; }
uv venv --python "$PY" .venv || exit 1
uv pip install --python .venv/bin/python -r requirements.lock.txt || exit 1
echo "--- functional probe (NOT an import-existence check) ---"
./.venv/bin/python -c "
import boto3, zstandard, yaml, sqlite3
from zoneinfo import ZoneInfo
from datetime import datetime
zstandard.ZstdCompressor().compress(b'x')
sqlite3.connect(':memory:').execute('select 1')
print('resolved interpreter:', __import__('os').path.realpath(__import__('sys').executable))
print('melbourne now:', datetime.now(ZoneInfo('Australia/Melbourne')).isoformat())
print('OK')"
