"""Patch only an ephemeral GitHub Actions runtime; never a user's Python."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

URL = 'https://sqlite.org/2026/sqlite-dll-win-x64-3510300.zip'
SHA256 = 'ea004f52ba873cc54d1cb908c2f7cf48c3064061d9ee3ded6a309284da5ff411'


def main():
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('This helper is restricted to ephemeral Windows GitHub runners.')
    cache = Path(os.environ['RUNNER_TOOL_CACHE']).resolve()
    base = Path(sys.base_prefix).resolve()
    if not base.is_relative_to(cache) or base == cache:
        raise SystemExit('Refusing to change a runtime outside RUNNER_TOOL_CACHE')
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('SQLite archive checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        dll = archive.read('sqlite3.dll')
    (base/'DLLs/sqlite3.dll').write_bytes(dll)
    subprocess.run([sys.executable, '-c', 'from disk_downloader.sqlite_support import require_safe; require_safe(); import sqlite3; print(sqlite3.sqlite_version)'], check=True)


if __name__ == '__main__':
    main()

