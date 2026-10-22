import errno
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import make_job, run_job, test_timeout, wait_job
from disk_downloader.api import APIError
from disk_downloader.destination import DestinationGuard, storage_error
from disk_downloader.engine import Control
from disk_downloader.folder_usage import FolderUsage


def until(predicate):
    due = time.monotonic()+test_timeout(4)
    while time.monotonic() < due:
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Expected storage state was not reached')


def recovery_fixture(system):
    engine, _, tmp = system
    job = make_job(system)
    control = Control()
    control.phase = 'preparing'
    control.storage_retry_delays = (.03, .06, .12, .2)
    online = threading.Event()
    online.set()
    identity = [(1, 2)]
    usage = [shutil.disk_usage(tmp)]
    calls = []
    def probe():
        calls.append(time.monotonic())
        if not online.is_set():raise OSError(errno.EIO, 'private path and credentials must not leak')
        return identity[0], usage[0]
    control.destination = DestinationGuard(engine.store.job(job)['root'], probe=probe)
    assert control.destination.check(force=True)[1] == ''
    engine.controls[job] = control
    return engine, job, control, online, identity, usage, calls


def stop_recovery(control):
    thread = control.storage_retry_thread
    control.stop()
    if thread:thread.join(test_timeout(3))
    assert not thread or not thread.is_alive()


def test_destination_backoff_and_automatic_recovery(system):
    engine, job, control, online, _, _, calls = recovery_fixture(system)
    assert Control().storage_retry_delays == (2, 4, 8, 16, 32, 60)
    try:
        online.clear()
        assert engine.check_space(job, control, force=True)
        until(lambda: len(calls) >= 4)
        assert control.paused and control.storage_snapshot()['automatic']
        assert calls[2]-calls[1] >= .025
        assert calls[3]-calls[2] >= .05
        online.set()
        until(lambda: not control.paused)
        assert engine.store.job(job)['phase'] == 'preparing'
        assert control.pause_reason == '' and not control.storage_snapshot()['waiting']
    finally:stop_recovery(control)


@pytest.mark.parametrize('action', ['pause', 'stop'])
def test_user_can_cancel_storage_recovery_without_waiting_for_retry(system, action):
    engine, job, control, online, _, _, _ = recovery_fixture(system)
    control.storage_retry_delays = (60,)
    try:
        online.clear()
        engine.check_space(job, control, force=True)
        until(lambda: control.storage_retry_due is not None)
        thread = control.storage_retry_thread
        getattr(control, action)()
        online.set()
        thread.join(test_timeout(.5))
        assert not thread.is_alive()
        assert control.stopped if action == 'stop' else control.paused and control.pause_kind == 'user'
        assert not control.storage_snapshot()['automatic']
    finally:stop_recovery(control)


def test_manual_pause_during_successful_probe_is_never_undone(system):
    engine, job, control, online, identity, usage, _ = recovery_fixture(system)
    entered, release = threading.Event(), threading.Event()
    original = control.destination.probe
    try:
        online.clear()
        engine.check_space(job, control, force=True)
        def delayed():
            entered.set()
            assert release.wait(test_timeout(3))
            return identity[0], usage[0]
        control.destination.probe = delayed
        assert entered.wait(test_timeout(2))
        control.pause()
        release.set()
        until(lambda: control.storage_retry_thread is None)
        assert control.paused and control.pause_kind == 'user'
        assert engine.store.job(job)['phase'] == 'paused'
    finally:
        release.set()
        control.destination.probe = original
        stop_recovery(control)


def test_recovery_never_accepts_another_volume_or_folder(system):
    engine, job, control, online, identity, _, calls = recovery_fixture(system)
    try:
        online.clear()
        engine.check_space(job, control, force=True)
        identity[0] = (9, 9)
        online.set()
        until(lambda: control.pause_kind == 'storage_manual')
        assert control.paused and 'другой накопитель' in control.pause_reason
        count = len(calls)
        time.sleep(.15)
        assert len(calls) == count
        assert not control.storage_snapshot()['automatic']
    finally:stop_recovery(control)


def test_recovery_respects_hard_free_space_floor(system):
    engine, job, control, online, _, usage, _ = recovery_fixture(system)
    try:
        online.clear()
        engine.check_space(job, control, force=True)
        old = usage[0]
        usage[0] = type(old)(old.total, old.total-1024, 1024)
        online.set()
        until(lambda: control.pause_kind == 'space')
        assert control.paused and '500 МБ' in control.pause_reason
        assert not control.storage_snapshot()['automatic']
    finally:stop_recovery(control)


def test_storage_error_explains_source_without_raw_paths():
    message = storage_error(OSError(errno.EIO, 'secret NAS path'))
    assert 'Накопитель' in message and 'ОС: 5' in message
    assert 'запретили доступ' not in message and 'secret NAS path' not in message
    assert 'запретили доступ' in storage_error(PermissionError(errno.EACCES, 'secret'))
    assert 'не найдена' in storage_error(FileNotFoundError(errno.ENOENT, 'secret'))
    error = OSError('secret');error.winerror = 64
    assert 'Пропала связь' in storage_error(error) and 'Windows: 64' in storage_error(error)


def test_session_counts_only_new_results_and_survives_page_refresh(system):
    engine, api, _ = system
    api.file('disk:/one.bin', b'first', preview=False)
    api.file('disk:/two.bin', b'second', preview=False)
    first = run_job(system, mode='fallback')
    assert first['session']['originals'] == 2 and first['session']['saved_bytes'] == 11
    api.file('disk:/three.bin', b'new data', preview=False)
    job = engine.create({'operation':'missing', 'destination':first['root'], 'create_zip':False})
    ready = wait_job(engine, job)
    assert ready['originals'] == 2 and ready['session']['originals'] == 0
    assert ready['session']['checked_files'] == 3
    engine.action(job, 'start')
    done = wait_job(engine, job)
    assert done['originals'] == 3
    assert done['session']['originals'] == 1 and done['session']['previews'] == 0
    assert done['session']['saved_bytes'] == done['session']['transferred_bytes'] == 8
    assert done['session']['id'] != ready['session']['id']
    page = engine.status_cache.snapshot(engine.store.job(job))
    again = engine.status_cache.snapshot(engine.store.job(job))
    assert page['session'] == again['session'] == done['session']


def test_fallback_success_is_not_a_session_error(system):
    engine, api, _ = system
    api.file('disk:/photo.jpg')
    api.fail[('disk:/photo.jpg','original')] = [APIError('restricted',403)]*5
    job = run_job(system,mode='fallback')
    assert job['session']['originals'] == 0 and job['session']['previews'] == 1
    assert job['session']['errors'] == job['errors'] == 0


def test_folder_size_publishes_partial_progress_without_scanning_logs(tmp_path):
    first = tmp_path/'photo-a';first.write_bytes(b'a'*20)
    second = tmp_path/'photo-b';second.write_bytes(b'b'*30)
    log = tmp_path/'huge.html';log.write_bytes(b'log'*10000)
    entered, release = threading.Event(), threading.Event()
    def inventory():
        yield first
        entered.set()
        assert release.wait(test_timeout(3))
        yield second
    usage = FolderUsage()
    try:
        usage.snapshot(str(tmp_path),inventory=('job',inventory))
        assert entered.wait(test_timeout(2))
        partial = usage.snapshot(str(tmp_path),inventory=('job',inventory))
        assert partial['scanning'] and partial['partial'] and partial['bytes'] == 20
        assert partial['counted_files'] == 1
        release.set()
        until(lambda: not usage.snapshot(str(tmp_path),inventory=('job',inventory))['scanning'])
        assert usage.snapshot(str(tmp_path),inventory=('job',inventory))['bytes'] == 50
        assert log.stat().st_size == 30000
    finally:release.set()


def test_statistics_do_not_read_unresponsive_nas_in_calling_thread(system, monkeypatch):
    engine, api, _ = system
    api.file('disk:/file.bin', b'kept', preview=False)
    job = run_job(system,mode='fallback')
    owner = threading.get_ident()
    size, disk = __import__('os').path.getsize, shutil.disk_usage
    def guard_size(*args, **kwargs):
        assert threading.get_ident() != owner, 'Statistics called filesystem size synchronously'
        return size(*args, **kwargs)
    def guard_disk(*args, **kwargs):
        assert threading.get_ident() != owner, 'Statistics called NAS disk usage synchronously'
        return disk(*args, **kwargs)
    monkeypatch.setattr('os.path.getsize',guard_size)
    monkeypatch.setattr(shutil,'disk_usage',guard_disk)
    with engine.cached_storage_status():
        result = engine.status(job['id'])
    assert result['originals'] == 1 and result['saved_bytes'] == 4
    assert result['local_bytes'] == job['local_bytes']


def test_missing_verification_publishes_counts_before_a_slow_next_file(system, monkeypatch):
    engine, api, _ = system
    for i in range(3):api.file(f'disk:/{i}.bin',b'kept',preview=False)
    first = run_job(system,mode='fallback')
    entered, release = threading.Event(), threading.Event()
    reusable = engine.reusable
    def slow(job, entry, kind, meta, control):
        if kind == 'original' and entry['path'] == 'disk:/0.bin':time.sleep(.55)
        if kind == 'original' and entry['path'] == 'disk:/1.bin':
            entered.set()
            assert release.wait(test_timeout(4))
        return reusable(job,entry,kind,meta,control)
    monkeypatch.setattr(engine,'reusable',slow)
    job = engine.create({'operation':'missing','destination':first['root']})
    try:
        assert entered.wait(test_timeout(3))
        page = engine.status_cache.snapshot(engine.store.job(job))
        assert page['verification']['checked'] == 1 and page['verification']['originals'] == 1
        assert page['session']['checked_files'] == 1
        with engine.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND status='existing'",(job,)).fetchone()[0] == 1
    finally:release.set()
    assert wait_job(engine,job)['existing'] == 3


def test_stop_with_offline_destination_skips_sidecar_writes_and_keeps_database(system, monkeypatch):
    from disk_downloader.journal import Journal
    engine, api, _ = system
    api.file('disk:/kept.bin',b'kept',preview=False)
    job = make_job(system,mode='fallback')
    control = Control()
    control.phase = 'preparing'
    control.destination = DestinationGuard(engine.store.job(job)['root'],probe=lambda:(_ for _ in ()).throw(OSError(errno.EIO,'offline')))
    control.destination.check(force=True)
    control.stop()
    engine.controls[job] = control
    journal = Journal(engine,job)
    monkeypatch.setattr('disk_downloader.journal.temporary_file',lambda *a:pytest.fail('Offline destination must not receive a new journal'))
    journal.close()
    assert 'События сохранены' in journal.error
    assert engine.store.job(job)['id'] == job


def test_preview_probe_failure_keeps_a_verified_original(system, monkeypatch):
    engine, api, _ = system
    api.file('disk:/photo.jpg')
    first = run_job(system,mode='both')
    job = engine.store.job(first['id'])
    job['settings'].update(operation='missing',missing_policy='both')
    reusable = engine.reusable
    def unavailable(job,entry,kind,meta,control):
        if kind == 'preview':raise ValueError('invalid preview record')
        return reusable(job,entry,kind,meta,control)
    monkeypatch.setattr(engine,'reusable',unavailable)
    engine.check_local_results(job,Control())
    with engine.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE job=? AND type='original'",(job['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT original_status FROM entries WHERE job=? AND kind=\'file\'',(job['id'],)).fetchone()[0] == 'existing_verified'


def test_disconnect_at_publish_retries_in_same_worker_without_false_error(system, monkeypatch):
    import disk_downloader.engine as module
    engine, api, _ = system
    content = b'complete verified original'*200
    api.file('disk:/photo.bin',content,preview=False)
    job = make_job(system,mode='fallback')
    online = threading.Event();online.set()
    retry_entered, release_retry = threading.Event(), threading.Event()
    read, publish, metadata = DestinationGuard.read, module.publish, api.metadata
    failed, calls = [], []
    def probe(guard):
        if not online.is_set():raise OSError(errno.EIO,'simulated mount loss')
        return read(guard)
    def interrupted(source,target):
        if not failed:
            failed.append(True);online.clear();raise OSError(errno.EIO,'simulated mount loss')
        return publish(source,target)
    def rechecked(path,settings=None,checkpoint=lambda:None,**kwargs):
        if path == 'disk:/photo.bin':
            calls.append(threading.get_ident())
            if len(calls) == 2:
                retry_entered.set()
                while not release_retry.wait(.02):checkpoint()
        return metadata(path,settings,checkpoint,**kwargs)
    monkeypatch.setattr(DestinationGuard,'read',probe)
    monkeypatch.setattr(module,'publish',interrupted)
    monkeypatch.setattr(api,'metadata',rechecked)
    engine.action(job,'start',{'workers':2,'attempts':1,'create_zip':False})
    control = engine.controls[job]
    try:
        until(lambda: control.storage_snapshot()['waiting'])
        control.storage_retry_delays = (.03,)
        with control.condition:
            control.storage_retry_due = time.monotonic()+.03
            control.condition.notify_all()
        online.set()
        assert retry_entered.wait(test_timeout(3))
        time.sleep(.45)  # Scheduler gets more than two opportunities to submit.
        with engine.store.db() as db:
            assert db.execute('SELECT status FROM entries WHERE job=? AND kind=\'file\'',(job,)).fetchone()[0] == 'running'
        assert len(calls) == 2 and calls[0] == calls[1]
        release_retry.set()
        done = wait_job(engine,job)
        assert done['phase'] == 'completed' and done['errors'] == done['session']['errors'] == 0
        assert done['originals'] == done['session']['originals'] == 1
        assert done['session']['transferred_bytes'] == len(content)
        assert api.media_calls == [('disk:/photo.bin','original')]
        artifact = next(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(job,)))
        assert (Path(done['root'])/artifact['local']).read_bytes() == content
        with engine.store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM pending_publications WHERE job=?',(job,)).fetchone()[0] == 0
    finally:
        online.set();release_retry.set();control.stop()


def test_session_and_storage_controls_render_without_browser_defaults():
    node = shutil.which('node')
    if not node:pytest.skip('Node.js unavailable')
    source = Path('disk_downloader/static/app.js').read_text(encoding='utf-8')
    functions = source[source.index('function renderSessionCounters('):source.index('function renderEstimate(')]
    script = """
const assert=require('node:assert/strict'),elements={},rows=[];
const $=id=>elements[id]??=( {replaceChildren(){},hidden:false,textContent:''} );
const bytes=n=>String(n),counter=(holder,label,value)=>rows.push([label,value]);
"""+functions+"""
renderSessionCounters({session:{started_at:'2026-01-01T10:00:00Z',elapsed_seconds:61,originals:2,previews:1,transferred_bytes:123,saved_bytes:100,errors:0},speed:4,verification:{checked:55,total:100,originals:50,previews:5,complete:false}});
assert.ok(rows.some(([label,value])=>label==='Новых оригиналов'&&value===2));
assert.ok($('verification-live').textContent.includes('55 / 100'));
renderStorage({storage:{waiting:true,automatic:true,retry_in:7.2,message:'Нет связи с NAS'}});
assert.equal($('storage-warning').hidden,false);
assert.ok($('storage-retry-status').textContent.includes('через 8 с'));
assert.equal($('stop-storage-retry').hidden,false);
renderStorage({storage:{waiting:true,automatic:false,message:'Другая папка'}});
assert.equal($('stop-storage-retry').hidden,true);
renderStorage({storage:{waiting:false}});assert.equal($('storage-warning').hidden,true);
"""
    subprocess.run([node,'-e',script],check=True,timeout=test_timeout(10))


def test_destination_reparse_point_cannot_redirect_automatic_writes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    import disk_downloader.destination as destination
    from disk_downloader.destination import UnsafeDestination
    guard = DestinationGuard(str(tmp_path))
    monkeypatch.setattr(destination,'os',SimpleNamespace(stat=lambda *a,**k:SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400),path=SimpleNamespace(islink=lambda *a:False)))
    monkeypatch.setattr(destination,'shutil',SimpleNamespace(disk_usage=lambda *a:pytest.fail('Do not follow an untrusted reparse point')))
    with pytest.raises(UnsafeDestination):guard.read()



def test_session_duration_excludes_pause_even_when_stopped_while_paused(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('disk_downloader.engine.time.monotonic',lambda:now[0])
    control = Control()
    now[0] = 110
    control.pause()
    now[0] = 200
    assert control.snapshot()['session']['elapsed_seconds'] == 10
    control.stop()
    now[0] = 201
    control.finished = now[0]
    assert control.snapshot()['session']['elapsed_seconds'] == 10
