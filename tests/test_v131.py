import shutil
import threading
import zipfile
from pathlib import Path

from conftest import run_job, wait_job, test_timeout
from disk_downloader.engine import Engine
from disk_downloader.store import Store
from tools.package_release import package


def test_portable_originals_count_before_start_without_media_requests(system, tmp_path):
    engine, api, _ = system
    api.file('disk:/kept.bin', b'keep', preview=False)
    first = run_job(system, mode='fallback')
    copied = tmp_path / 'copied'
    shutil.copytree(first['root'], copied)
    api.file('disk:/new.bin', b'new', preview=False)
    api.media_calls.clear()
    restored = Engine(Store(tmp_path / 'fresh-state' / 'jobs.sqlite3'), api)
    job_id = restored.create({'operation': 'missing', 'destination': str(copied)})
    ready = wait_job(restored, job_id)
    assert ready['phase'] == 'ready'
    assert ready['existing'] == ready['processed'] == ready['previous_results'] == 1
    assert ready['percent'] == 50
    assert ready['remaining_work']['files'] == 1
    assert api.media_calls == []
    restored.action(job_id, 'start')
    done = wait_job(restored, job_id)
    assert done['phase'] == 'completed'
    assert api.media_calls == [('disk:/new.bin', 'original')]


def test_windows_distribution_has_only_gui_exe_and_no_private_state(tmp_path):
    binary=tmp_path/'binary'/'YaDiskDownloader.exe'
    binary.parent.mkdir()
    binary.write_bytes(b'example-gui-exe')
    (binary.parent/'YaDiskDownloader-CLI.exe').write_bytes(b'stale-cli-must-not-ship')
    (binary.parent/'THIRD_PARTY_NOTICES.txt').write_text('licenses',encoding='utf-8')
    wheels=binary.parent/'third_party'
    wheels.mkdir()
    (wheels/'pystray-0.19.5-py2.py3-none-any.whl').write_bytes(b'test-source-wheel')
    output=tmp_path/'release'
    files=package(output,binary)
    assert len(files)==1 and files[0].name.endswith('-Windows-x64.zip')
    assert list(output.iterdir())==files
    with zipfile.ZipFile(files[0]) as archive:
        names=set(archive.namelist())
        assert archive.read('YaDiskDownloader/YaDiskDownloader.exe')==b'example-gui-exe'
        assert not any(name.endswith(('.cmd','.ps1')) or 'YaDiskDownloader-CLI' in name for name in names)
        assert not any(any(part in ('.state','.venv','__pycache__') for part in Path(name).parts) for name in names)
        assert archive.testzip() is None


def test_untracked_original_is_in_progress_before_start(system):
    engine, api, tmp = system
    api.file('disk:/file.bin', b'verified', preview=False)
    root = tmp / 'old-results'
    root.mkdir()
    (root / 'file.bin').write_bytes(b'verified')
    job_id = engine.create({'operation': 'missing', 'destination': str(root), 'whole_disk': True})
    ready = wait_job(engine, job_id)
    assert ready['phase'] == 'ready'
    assert ready['processed'] == ready['existing'] == 1
    assert ready['percent'] == 100 and ready['remaining_work']['files'] == 0
    assert api.media_calls == []


def test_preflight_finishes_all_files_before_download_and_rechecks_ready_files(system, monkeypatch):
    engine, api, _ = system
    for name in ('a.bin', 'b.bin'):
        api.file('disk:/' + name, name.encode(), preview=False)
    first = run_job(system, mode='fallback')
    api.file('disk:/new.bin', b'new', preview=False)
    job_id = engine.create({'operation': 'missing', 'destination': first['root'], 'workers': 1})
    assert wait_job(engine, job_id)['existing'] == 2
    Path(first['root'], 'b.bin').unlink()
    entered, release = threading.Event(), threading.Event()
    original = engine.reusable

    def blocked(job, entry, kind, meta, control):
        if entry['path'] == 'disk:/new.bin' and kind == 'original':
            entered.set()
            while not release.wait(0.01):
                control.check()
        return original(job, entry, kind, meta, control)

    monkeypatch.setattr(engine, 'reusable', blocked)
    api.media_calls.clear()
    engine.action(job_id, 'start')
    try:
        assert entered.wait(test_timeout(10))
        progress = engine.status(job_id)
        assert progress['phase'] == 'preparing'
        assert progress['existing'] == 1
        assert api.media_calls == []
        assert progress['active'][0]['action'] == 'Проверка скачанной папки'
    finally:
        release.set()
    done = wait_job(engine, job_id)
    assert done['phase'] == 'completed'
    assert api.media_calls == [('disk:/b.bin', 'original'), ('disk:/new.bin', 'original')]


def test_preflight_counts_preview_but_keeps_upgrade_pending_and_rejects_corruption(system):
    engine, api, _ = system
    api.file('disk:/photo.jpg')
    api.file('disk:/broken.jpg')
    first = run_job(system)
    damaged = Path(first['root'], 'broken.jpg.preview.png')
    damaged.write_bytes(b'x' * damaged.stat().st_size)
    api.media_calls.clear()
    job_id = engine.create({'operation': 'missing', 'destination': first['root']})
    ready = wait_job(engine, job_id)
    assert ready['phase'] == 'ready'
    assert ready['previous_results'] == ready['previews'] == 1
    assert ready['processed'] == 0 and ready['remaining_work']['files'] == 2
    entries = {row['path']: row for row in engine.store.rows('SELECT path,priority,preview_status FROM entries WHERE job=? AND kind=\'file\'', (job_id,))}
    assert entries['disk:/photo.jpg']['priority'] == 1
    assert entries['disk:/photo.jpg']['preview_status'] == 'existing_verified'
    assert entries['disk:/broken.jpg']['priority'] == 0
    assert api.media_calls == []
