"""Use a pinned, patched SQLite in CI without changing system libraries."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import urllib.request
import zipfile

URL = 'https://sqlite.org/2026/sqlite-amalgamation-3510300.zip'
SHA256 = 'acb1e6f5d832484bf6d32b681e858c38add8b2acdfd42ac5df24b8afb46552b4'


def main():
    if os.name != 'posix' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('This helper is restricted to ephemeral Linux GitHub runners.')
    target = Path(os.environ['RUNNER_TEMP']).resolve()/'yadisk-sqlite'
    target.mkdir(exist_ok=True)
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('SQLite source checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        source = archive.read('sqlite-amalgamation-3510300/sqlite3.c')
    (target/'sqlite3.c').write_bytes(source)
    subprocess.run(['gcc','-O2','-fPIC','-shared','-DSQLITE_THREADSAFE=1',
                    '-DSQLITE_ENABLE_COLUMN_METADATA','-DSQLITE_ENABLE_FTS5',
                    '-DSQLITE_ENABLE_RTREE','-Wl,-soname,libsqlite3.so.0',
                    '-o',str(target/'libsqlite3.so.0'), str(target/'sqlite3.c'),'-lpthread','-ldl','-lm'], check=True)
    value = str(target) + (':'+os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else '')
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
        stream.write('LD_LIBRARY_PATH='+value+'\n')


if __name__ == '__main__':
    main()
