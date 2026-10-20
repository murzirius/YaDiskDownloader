import errno
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from conftest import make_job, run_job, wait_job, test_timeout
from disk_downloader.api import APIError
from disk_downloader.destination import DestinationGuard
from disk_downloader.engine import Control
from disk_downloader.estimation import VerificationTime


def test_blocked_destination_probe_is_bounded_and_cancellable(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls=[]
    def probe():
        calls.append(1);entered.set();assert release.wait(test_timeout(5))
        return (1,2),shutil.disk_usage(tmp_path)
    guard=DestinationGuard(str(tmp_path),probe=probe,timeout=.03)
    try:
        value,error=guard.check()
        assert entered.is_set() and value is None and 'не отвечает' in error
        for _ in range(4):assert guard.check(force=True)[0] is None
        assert len(calls)==1
        start=time.monotonic();guard.check(stopped=lambda:True)
        assert time.monotonic()-start<.2
    finally:release.set()
    with guard.condition:assert guard.condition.wait_for(lambda:not guard.running,test_timeout(5))
    assert guard.check()[1]==''


def test_destination_replacement_is_not_accepted(tmp_path):
    identity=[(1,2)]
    guard=DestinationGuard(str(tmp_path),probe=lambda:(identity[0],shutil.disk_usage(tmp_path)))
    assert not guard.check()[1]
    identity[0]=(1,3)
    assert 'другая папка' in guard.check(force=True)[1]
    identity[0]=(1,2)
    assert not guard.check(force=True)[1]


def test_lost_destination_keeps_verified_history_until_resume(system, monkeypatch):
    import disk_downloader.engine as module
    engine,api,_=system
    api.file('disk:/photo.bin',b'kept content',preview=False)
    first=run_job(system,mode='fallback')
    before=(Path(first['root'])/'photo.bin').read_bytes()
    original_hash=module.file_hash
    original_read=DestinationGuard.read
    offline=threading.Event();lost=threading.Event()
    def read(guard):
        if offline.is_set():raise OSError(errno.EIO,'simulated NAS disconnect')
        return original_read(guard)
    def hashing(filename,checkpoint=lambda:None):
        if not lost.is_set() and str(filename).endswith('photo.bin'):
            lost.set();offline.set();raise OSError(errno.EIO,'simulated NAS disconnect')
        return original_hash(filename,checkpoint)
    monkeypatch.setattr(DestinationGuard,'read',read)
    monkeypatch.setattr(module,'file_hash',hashing)
    api.media_calls.clear()
    job_id=engine.create({'destination':first['root'],'operation':'missing','verification_mode':'full'})
    try:
        paused=wait_job(engine,job_id,phases={'paused'})
        assert lost.is_set() and 'Папка загрузки' in paused['pause_reason']
        with engine.store.db() as db:assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job_id,)).fetchone()[0]==1
        assert (Path(first['root'])/'photo.bin').read_bytes()==before
        offline.clear();engine.action(job_id,'resume')
        done=wait_job(engine,job_id)
        assert done['phase']=='ready' and done['originals']==1 and api.media_calls==[]
    finally:
        offline.clear();engine.controls[job_id].stop()


def test_stop_is_prompt_during_unresponsive_destination(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job_id=make_job(system,mode='fallback')
    entered,release=threading.Event(),threading.Event()
    original=DestinationGuard.read
    def blocked(guard):
        entered.set();assert release.wait(test_timeout(5));return original(guard)
    monkeypatch.setattr(DestinationGuard,'read',blocked)
    engine.action(job_id,'start')
    try:
        assert entered.wait(test_timeout(5))
        start=time.monotonic();engine.action(job_id,'stop')
        assert wait_job(engine,job_id)['phase']=='stopped'
        assert time.monotonic()-start<test_timeout(2)
    finally:release.set()


def test_retry_failed_does_not_upgrade_successful_preview(system):
    engine,api,_=system
    for name in ('good','preview','bad'):api.file(f'disk:/{name}.jpg')
    api.fail[('disk:/preview.jpg','original')]=[APIError('unavailable',503)]*2
    api.fail[('disk:/bad.jpg','original')]=[APIError('unavailable',503)]*2
    api.fail[('disk:/bad.jpg','preview')]=[APIError('unavailable',503)]*2
    first=run_job(system,mode='fallback')
    assert first['errors']==1 and first['completeness']['preview_only']==1
    api.media_calls.clear();engine.action(first['id'],'retry_failed');done=wait_job(engine,first['id'])
    assert done['errors']==0 and done['originals']==2 and done['completeness']['preview_only']==1
    assert api.media_calls==[('disk:/bad.jpg','original')]
    with engine.store.db() as db:assert db.execute('SELECT COUNT(*) FROM run_selection').fetchone()[0]==0


def test_verification_eta_does_not_count_stalled_time_as_speed():
    clock=[0.0];eta=VerificationTime(lambda:clock[0])
    assert eta.estimate(0,100) is None
    for second in range(1,4):clock[0]=second;value=eta.estimate(second*10,100)
    assert value==7
    clock[0]=14
    assert eta.estimate(30,100) is None
    assert eta.estimate(100,100)==0


def test_health_tracks_actual_progress_and_does_not_warn_after_completion(monkeypatch):
    import disk_downloader.engine as module
    now=[1.0];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    control=Control();control.progress('file',1,100,'Загрузка')
    now[0]=122;control.progress('file',1,100,'Загрузка')
    assert control.health()['warning']
    control.progress('file',2,100,'Загрузка');assert not control.health()['warning']
    now[0]=244;control.finished=now[0];assert not control.health()['warning']


def test_activation_uses_existing_csrf_and_matches_state_folder(tmp_path):
    from werkzeug.serving import make_server
    from disk_downloader.web import create_app
    from disk_downloader.single_instance import activate_existing
    import socket
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    app=create_app(tmp_path/'state',port=port)
    calls=[];app.extensions['activate_window']=lambda:calls.append(True)
    server=make_server('127.0.0.1',port,app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        assert activate_existing(tmp_path/'state',port) and calls==[True]
        assert not activate_existing(tmp_path/'different',port) and calls==[True]
        client=app.test_client();base=f'http://127.0.0.1:{port}'
        assert client.post('/api/activate',base_url=base,headers={'Origin':base}).status_code==403
    finally:server.shutdown();server.server_close();thread.join(5)


def test_failed_job_thread_start_keeps_task_resumable(system,monkeypatch):
    engine,api,_=system;api.file('disk:/one.bin',preview=False)
    job=make_job(system,mode='fallback')
    with monkeypatch.context() as denied:
        denied.setattr(threading.Thread,'start',lambda self:(_ for _ in ()).throw(RuntimeError('no thread')))
        with pytest.raises(ValueError,match='не смогло начать'):engine.action(job,'start')
    assert not engine.threads.get(job)
    assert engine.store.job(job)['phase']=='interrupted'
    engine.action(job,'resume');assert wait_job(engine,job)['phase']=='completed'


def test_large_journal_search_yields_and_discards_stale_search():
    import subprocess
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable')
    subprocess.run([node,'tests/journal_filter_worker.cjs'],check=True,timeout=15)


def test_expected_source_wait_does_not_trigger_stall_warning(monkeypatch):
    import disk_downloader.engine as module
    now=[1.0];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    control=Control()
    with control.expected_wait():
        now[0]=500
        assert not control.health()['warning']
    assert not control.health()['warning']


def test_download_pause_does_not_show_completed_preflight_progress():
    control=Control();control.verification={'checked':10,'total':10,'complete':True}
    control.phase='downloading'
    assert control.snapshot()['verification'] is None
    control.phase='preparing'
    assert control.snapshot()['verification']['checked']==10


def test_api_md5_compatibility_works_when_security_use_is_disabled(system,monkeypatch):
    import hashlib
    engine,api,_=system;api.file('disk:/one.bin',b'file',preview=False)
    original=hashlib.md5
    def fips_md5(*args,**kwargs):
        if kwargs.get('usedforsecurity') is not False:raise ValueError('MD5 disabled for security use')
        return original(*args,**kwargs)
    monkeypatch.setattr(hashlib,'md5',fips_md5)
    result=run_job(system,mode='fallback')
    assert result['phase']=='completed' and result['originals']==1


def test_offline_check_does_not_report_disconnected_nas_as_missing_copies(system,monkeypatch):
    import disk_downloader.maintenance as module
    engine,api,_=system;api.file('disk:/one.bin',b'keep',preview=False)
    job=run_job(system,mode='fallback')
    original=DestinationGuard.read;offline=threading.Event()
    def read(guard):
        if offline.is_set():raise OSError(errno.EIO,'NAS offline')
        return original(guard)
    def signature(*args):offline.set();raise OSError(errno.EIO,'NAS offline')
    monkeypatch.setattr(DestinationGuard,'read',read);monkeypatch.setattr(module,'file_signature',signature)
    maintenance=module.Maintenance(engine);maintenance.start({'job':job['id']})
    maintenance.thread.join(test_timeout(5))
    result=maintenance.snapshot()
    assert not result['busy'] and not result['complete'] and 'Папка загрузки' in result['error']
    assert result['problems']==0 and engine.store.summary(job['id'])['originals']==1
    with pytest.raises(ValueError):maintenance.repair_settings()


def test_stall_notification_does_not_require_a_database_change(system):
    from types import SimpleNamespace
    from disk_downloader.tray import Tray
    engine,api,_=system;api.file('disk:/one.bin',preview=False)
    job=make_job(system);control=Control();control.last_progress-=121
    engine.controls[job]=control;engine.threads[job]=SimpleNamespace(is_alive=lambda:True)
    delivered=threading.Event()
    class Icon:
        visible=True
        def notify(self,text,title):
            if 'Давно нет прогресса' in text:delivered.set()
    tray=Tray(engine,'',lambda:None);tray.icon=Icon()
    thread=threading.Thread(target=tray.watch_notifications);thread.start()
    try:
        assert delivered.wait(test_timeout(5))
        assert engine.store.job(job)['phase']=='ready'
    finally:
        tray.notification_stop.set();thread.join(5);engine.threads.pop(job);engine.controls.pop(job)


def test_unexpected_verification_error_is_visible_and_releases_maintenance(system,monkeypatch):
    import disk_downloader.maintenance as module
    engine,api,_=system;api.file('disk:/one.bin',b'keep',preview=False)
    job=run_job(system,mode='fallback')
    def broken(*args):raise RuntimeError('unexpected file reader failure')
    monkeypatch.setattr(module,'file_signature',broken)
    maintenance=module.Maintenance(engine);maintenance.start({'job':job['id']})
    maintenance.thread.join(test_timeout(5))
    result=maintenance.snapshot()
    assert result['error'] and not result['complete'] and not result['busy']
    assert not engine.maintenance_busy
    with pytest.raises(ValueError):maintenance.repair_settings()


def test_recovery_never_swallows_persistent_database_io_failure(tmp_path,monkeypatch):
    import sqlite3
    import disk_downloader.backups as module
    failures=[];closed=[]
    class FailedSource:
        def execute(self,*args):
            failures.append(1)
            error=sqlite3.OperationalError('disk I/O error')
            error.sqlite_errorcode=sqlite3.SQLITE_IOERR_TRUNCATE
            raise error
        def close(self):closed.append(1)
    monkeypatch.setattr(module.sqlite3,'connect',lambda *args,**kwargs:FailedSource())
    monkeypatch.setattr(module.time,'sleep',lambda seconds:None)
    with pytest.raises(sqlite3.OperationalError):module.recovery_source(tmp_path/'missing.sqlite3')
    assert len(failures)==len(closed)==(4 if module.os.name=='nt' else 1)
