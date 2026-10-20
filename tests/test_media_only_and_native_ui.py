import builtins
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import run_job, wait_job, test_timeout
from disk_downloader.folder_usage import FolderUsage
from disk_downloader.paths import native
from disk_downloader.service_outputs import location, root_key


def await_usage(usage, store, job, **kwargs):
    end = time.monotonic() + test_timeout(5)
    value = usage.for_job(store, job['id'], job['root'], **kwargs)
    while value['scanning'] and time.monotonic() < end:
        time.sleep(.01)
        value = usage.for_job(store, job['id'], job['root'])
    assert not value['scanning']
    return value


def test_missing_download_never_reads_old_logs_or_walks_folders(system, monkeypatch):
    engine, api, _ = system
    api.file('disk:/kept.bin', b'keep', preview=False)
    first = run_job(system, mode='fallback')
    root = Path(first['root'])
    logs = root/'old-logs'
    logs.mkdir()
    for number in range(25):
        (logs/f'{number}.html').write_text('Old report', encoding='utf8')
    with engine.store.db() as db:
        db.executemany('INSERT INTO names VALUES(?,?,?,?)',
                       [(first['id'], '@output:old-'+str(n), f'old-logs/{n}.html', f'old-logs/{n}.html') for n in range(25)])
    log = root/engine.store.job(first['id'])['settings']['journal_local']
    # Simulate a legacy journal without a trustworthy local signature cache.
    original = log.read_bytes() + b' ' * 65536
    log.write_bytes(original)
    with engine.store.db() as db:
        db.execute('DELETE FROM service_output_signatures WHERE root=?', (root_key(root),))
    opened = builtins.open
    def guarded_open(filename, mode='r', *args, **kwargs):
        if isinstance(filename, (str, os.PathLike)) and 'r' in mode:
            filename_path = Path(filename)
            assert filename_path != Path(native(log)), 'Old journal content was read'
            assert filename_path.parent != Path(native(logs)), 'Old report content was read'
        return opened(filename, mode, *args, **kwargs)
    def no_walk(*args, **kwargs):
        raise AssertionError('Missing-file check must use inventory, not walk directories')
    monkeypatch.setattr(builtins, 'open', guarded_open)
    monkeypatch.setattr(os, 'scandir', no_walk)
    next_id = engine.create({'destination':str(root), 'operation':'missing', 'mode':'fallback'})
    ready = wait_job(engine, next_id)
    assert ready['phase'] == 'ready' and ready['originals'] == 1
    assert await_usage(engine.folder_usage, engine.store, ready, force=True)['bytes'] == 4
    engine.action(next_id, 'start')
    assert wait_job(engine, next_id)['phase'] == 'completed'
    assert api.media_calls == [('disk:/kept.bin', 'original')]
    # Path.open deliberately avoids the read guard for the final assertion.
    assert log.read_bytes() == original
    assert (root/'kept.bin').read_bytes() == b'keep'


def test_usage_counts_downloaded_log_filename_but_excludes_service_outputs(system):
    engine, api, _ = system
    api.file('disk:/log.html', b'downloaded log', preview=False)
    job = run_job(system, mode='fallback')
    (Path(job['root'])/'foreign.bin').write_bytes(b'x'*10000)
    usage = FolderUsage()
    result = await_usage(usage, engine.store, job, force=True)
    assert result['bytes'] == len(b'downloaded log')
    media = next(engine.store.rows('SELECT local FROM artifacts WHERE job=?', (job['id'],)))
    (Path(job['root'])/media['local']).unlink()
    assert await_usage(usage, engine.store, job, force=True)['bytes'] == 0


def test_shared_downloaded_copy_is_counted_once(system):
    engine, api, _ = system
    api.file('disk:/one.bin', b'copy', preview=False)
    job = run_job(system, mode='fallback')
    with engine.store.db() as db:
        artifact = db.execute('SELECT * FROM artifacts WHERE job=?', (job['id'],)).fetchone()
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)', tuple('disk:/alias.bin' if key=='path' else artifact[key] for key in artifact.keys()))
    assert await_usage(FolderUsage(), engine.store, job)['bytes'] == 4


def test_old_scope_cannot_overwrite_new_usage_snapshot(tmp_path):
    usage = FolderUsage()
    entered, release = threading.Event(), threading.Event()
    root = str(tmp_path)
    first = tmp_path/'first';first.write_bytes(b'one')
    second = tmp_path/'second';second.write_bytes(b'two-two')
    def blocked():
        entered.set()
        assert release.wait(test_timeout(5))
        yield first
    usage.snapshot(root, inventory=('old', blocked))
    try:
        assert entered.wait(test_timeout(5))
        usage.snapshot(root, inventory=('new', lambda: iter([second])))
    finally:
        release.set()
    end = time.monotonic() + test_timeout(5)
    while usage.folders[root]['scanning'] and time.monotonic() < end:time.sleep(.01)
    assert usage.folders[root]['bytes'] is None
    value = usage.snapshot(root, inventory=('new', lambda: iter([second])))
    while value['scanning'] and time.monotonic() < end:
        time.sleep(.01)
        value = usage.snapshot(root, inventory=('new', lambda: iter([second])))
    assert value['bytes'] == 7


def test_edited_journal_with_restored_mtime_is_not_overwritten(system):
    engine, api, _ = system
    api.file('disk:/one.bin', b'one', preview=False)
    job = run_job(system, mode='fallback')
    current = engine.store.job(job['id'])
    log = Path(job['root'])/current['settings']['journal_local']
    before = log.stat()
    log.write_bytes(b'x'*before.st_size)
    os.utime(log, ns=(before.st_atime_ns, before.st_mtime_ns))
    new_name, replacing = location(engine.store, current, 'journal', 'log.html')
    assert new_name != current['settings']['journal_local'] and not replacing
    assert log.read_bytes() == b'x'*before.st_size


def test_weak_filesystem_signature_never_authorizes_overwrite(system, monkeypatch):
    import disk_downloader.service_outputs as outputs
    engine, api, _ = system
    api.file('disk:/one.bin', preview=False)
    job = run_job(system, mode='fallback')
    current = engine.store.job(job['id'])
    signature = outputs.file_signature
    monkeypatch.setattr(outputs, 'file_signature', lambda file, sha: (*signature(file, sha)[:-1], None))
    new_name, replacing = location(engine.store, current, 'journal', 'log.html')
    assert new_name != current['settings']['journal_local'] and not replacing


def test_folder_button_immediately_uses_native_dialog():
    import re
    node = shutil.which('node')
    if not node:pytest.skip('Node.js is unavailable')
    js = Path('disk_downloader/static/app.js').read_text(encoding='utf8')
    function = re.search(r'async function chooseFolder\(.*?\}\n', js).group(0)
    script = "const assert=require('node:assert/strict');let initial; async function chooseNativeFolder(value){initial=value;return 'chosen';} function chooseFolderInApp(){throw Error('Intermediate dialog opened');}\n" + function + "\nchooseFolder('start').then(value=>{assert.equal(value,'chosen');assert.equal(initial,'start');});"
    subprocess.run([node, '-e', script], check=True, timeout=10)


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPI context')
def test_native_dialog_thread_uses_per_monitor_dpi_and_restores_context():
    import ctypes
    from disk_downloader.windows_dpi import native_thread
    user = ctypes.WinDLL('user32')
    user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.AreDpiAwarenessContextsEqual.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    user.AreDpiAwarenessContextsEqual.restype = ctypes.c_int
    result = []
    def worker():
        before = user.GetThreadDpiAwarenessContext()
        with native_thread():
            result.append(bool(user.AreDpiAwarenessContextsEqual(user.GetThreadDpiAwarenessContext(), ctypes.c_void_p(-4))))
        result.append(bool(user.AreDpiAwarenessContextsEqual(user.GetThreadDpiAwarenessContext(), before)))
    thread = threading.Thread(target=worker);thread.start();thread.join(5)
    assert not thread.is_alive() and result == [True, True]
