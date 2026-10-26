"""Storage recovery regressions; synthetic probes never access a real NAS."""
import errno
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from conftest import make_job, test_timeout
from disk_downloader.destination import DestinationGuard, network_path
from disk_downloader.diagnostics import bundle, storage_event_code
from disk_downloader.engine import Control
from test_storage_sessions import recovery_fixture, stop_recovery, until


@pytest.mark.parametrize('path,expected', [
    (r'\\server\share\folder',True),('//server/share/folder',True),
    (r'\\?\UNC\server\share',True),(r'C:\folder',False),(r'\\?\C:\folder',False),('/mnt/nas',False)])
def test_unc_detection(path,expected):
    assert network_path(path) is expected


def test_network_probe_accepts_response_slower_than_old_two_second_limit(tmp_path):
    def probe():
        time.sleep(2.15)
        return (1,2),shutil.disk_usage(tmp_path)
    guard=DestinationGuard(r'\\synthetic-server\share',probe=probe)
    value,error=guard.check()
    assert value and not error
    assert guard.timeout>=15 and guard.interval>=5
    assert guard.snapshot()['timeouts']==0


def test_timed_out_probe_stays_single_and_has_safe_diagnostics(tmp_path):
    release=threading.Event();calls=[]
    def probe():
        calls.append(1)
        assert release.wait(test_timeout(5))
        return (1,2),shutil.disk_usage(tmp_path)
    guard=DestinationGuard(r'\\PRIVATE-SERVER\private-folder',probe=probe,timeout=.03)
    try:
        for _ in range(3):
            value,error=guard.check(force=True)
            assert value is None and 'не отвечает' in error
        snapshot=guard.snapshot()
        assert len(calls)==1 and snapshot['timeouts']==1
        assert snapshot['state']=='timeout' and snapshot['error_origin']=='app_timeout'
        assert snapshot['error_code'] is None
        assert 'PRIVATE-SERVER' not in json.dumps(snapshot)
    finally:
        release.set()
        with guard.condition:guard.condition.wait_for(lambda:not guard.running,test_timeout(3))


def test_interactive_continue_consumes_late_result_instead_of_restarting_probe(tmp_path):
    release=threading.Event();calls=[]
    def probe():
        calls.append(1);assert release.wait(test_timeout(3))
        return (1,2),shutil.disk_usage(tmp_path)
    guard=DestinationGuard(str(tmp_path),probe=probe)
    try:
        assert guard.check(wait_timeout=.01)[0] is None
        release.set()
        with guard.condition:assert guard.condition.wait_for(lambda:not guard.running,test_timeout(3))
        assert guard.check(force=True,wait_timeout=.01)[0] is not None
        assert len(calls)==1
    finally:release.set()


def test_initial_unavailable_folder_retries_without_manual_continue(system):
    engine,job,control,online,_,_,_=recovery_fixture(system)
    guard=control.destination
    guard.identity=None;guard.value=None
    control.storage_confirmation_interval=.01
    try:
        online.clear();engine.check_space(job,control,force=True)
        assert control.pause_kind=='storage' and guard.snapshot()['recoverable']
        online.set();until(lambda:not control.paused)
        assert guard.identity is not None
        assert control.storage_confirmations>=2
    finally:stop_recovery(control)


def test_one_success_never_resumes_writes_and_flapping_preserves_backoff(system):
    engine,job,control,online,_,_,_=recovery_fixture(system)
    control.storage_confirmation_interval=.01
    control.storage_retry_delays=(.03,.06,.12,.3)
    try:
        online.clear();engine.check_space(job,control,force=True)
        online.set();until(lambda:control.storage_confirmations==1)
        assert control.paused and control.storage_snapshot()['confirming']
        online.clear();until(lambda:control.storage_confirmations==0)
        assert control.paused
        online.set();until(lambda:not control.paused)
        attempt=control.storage_retry_attempt
        assert attempt>=3
        online.clear();engine.check_space(job,control,force=True)
        assert control.storage_retry_attempt>=attempt
        assert control.paused
    finally:stop_recovery(control)


def test_backoff_reaches_cap_and_keeps_retrying(system):
    engine,job,control,online,_,_,calls=recovery_fixture(system)
    control.storage_retry_delays=(.01,.02)
    control.storage_confirmation_interval=.01
    try:
        online.clear();engine.check_space(job,control,force=True)
        until(lambda:len(calls)>=9)
        assert control.storage_retry_attempt>=7 and control.paused
        online.set();until(lambda:not control.paused)
    finally:stop_recovery(control)


def test_backoff_resets_only_after_healthy_window(system):
    engine,job,control,online,_,_,_=recovery_fixture(system)
    control.storage_retry_attempt=4
    control.storage_healthy_since=time.monotonic()
    control.storage_backoff_reset_after=1
    engine.check_space(job,control,force=True)
    assert control.storage_retry_attempt==4
    control.storage_healthy_since=time.monotonic()-2
    engine.check_space(job,control,force=True)
    assert control.storage_retry_attempt==0
    control.stop()


def test_manual_pause_cancels_long_probe_wait_and_resume_is_bounded(system):
    engine,job,control,_,identity,usage,_=recovery_fixture(system)
    entered,release=threading.Event(),threading.Event()
    def probe():
        entered.set();assert release.wait(test_timeout(5))
        return identity[0],usage[0]
    control.destination.probe=probe;control.destination.timeout=15
    worker=threading.Thread(target=lambda:engine.check_space(job,control,force=True))
    worker.start()
    try:
        assert entered.wait(test_timeout(2))
        control.pause()
        start=time.monotonic()
        assert engine.check_space(job,control,force=True,interactive=True)
        assert time.monotonic()-start<.5
        worker.join(test_timeout(1))
        assert not worker.is_alive() and control.paused and control.pause_kind=='user'
        assert control.storage_retry_thread is None
    finally:
        release.set();control.stop();worker.join(test_timeout(3))
        with control.destination.condition:control.destination.condition.wait_for(lambda:not control.destination.running,test_timeout(3))


def test_diagnostics_keep_storage_reason_and_operation_without_private_data(system):
    engine,job,control,online,_,_,_=recovery_fixture(system)
    control.storage_retry_delays=(60,)
    control.destination.root=r'\\PRIVATE-SERVER\private-folder'
    control.journal=None
    try:
        online.clear();engine.check_space(job,control,force=True)
        data=bundle(engine,job)
        storage=data['storage.json']
        assert storage['probe']['error_code']==errno.EIO
        assert storage['probe']['error_origin']=='os'
        assert storage['pause_kind']=='storage'
        text=json.dumps(data,ensure_ascii=False)
        assert 'PRIVATE-SERVER' not in text and 'credentials must not leak' not in text
        assert 'message' not in storage and 'error' not in storage['probe']
    finally:stop_recovery(control)


def test_storage_event_codes_do_not_export_messages():
    assert storage_event_code('Накопитель снова доступен. Прежняя папка проверена')=='storage_resumed'
    assert storage_event_code('Накопитель или NAS не отвечает вовремя: проверка папки.')=='storage_probe_wait'
    assert storage_event_code('Папка загрузки недоступна. Код Windows: 64.')=='storage_unavailable'
    assert storage_event_code('arbitrary private text') is None


def test_folder_and_free_space_timings_are_separate(tmp_path):
    guard=DestinationGuard(str(tmp_path))
    assert guard.check()[0]
    data=guard.snapshot()
    assert data['folder_seconds'] is not None and data['space_seconds'] is not None
    assert data['stage']=='space' and data['state']=='available'


def test_free_space_error_does_not_claim_folder_disappeared(tmp_path,monkeypatch):
    import disk_downloader.destination as module
    guard=DestinationGuard(str(tmp_path))
    def failed(*args):raise PermissionError(errno.EACCES,'PRIVATE-SERVER')
    monkeypatch.setattr(module.shutil,'disk_usage',failed)
    value,error=guard.check()
    assert value is None and 'Папка найдена' in error
    assert 'Папка загрузки недоступна' not in error and 'PRIVATE-SERVER' not in error
    data=guard.snapshot()
    assert data['stage']=='space' and data['error_origin']=='os'
    assert data['folder_seconds'] is not None
    assert data['error_code']==errno.EACCES


def test_http_commands_do_not_wait_for_blocked_destination(tmp_path,monkeypatch):
    from conftest import FakeAPI,wait_job
    from disk_downloader.web import create_app
    api=FakeAPI();api.file('disk:/one.bin',b'synthetic',preview=False)
    app=create_app(tmp_path/'state',api)
    if app.extensions['recovery'].thread:
        app.extensions['recovery'].thread.join(test_timeout(3))
    engine=app.extensions['engine']
    job=engine.create({'destination':str(tmp_path/'downloads'),'paths':['disk:/'],'create_zip':False})
    assert wait_job(engine,job)['phase']=='ready'
    entered,release=threading.Event(),threading.Event()
    original=DestinationGuard.read
    def blocked(guard):
        guard.timeout=15
        entered.set();assert release.wait(test_timeout(5))
        return original(guard)
    monkeypatch.setattr(DestinationGuard,'read',blocked)
    client=app.test_client();base='http://127.0.0.1:8766'
    client.get('/',base_url=base)
    with client.session_transaction(base_url=base) as session:csrf=session['csrf']
    headers={'Origin':base,'X-CSRF-Token':csrf}
    engine.action(job,'start')
    try:
        assert entered.wait(test_timeout(2))
        for action in ('pause','resume','stop'):
            start=time.monotonic()
            response=client.post('/api/jobs/'+job+'/action',json={'action':action},base_url=base,headers=headers)
            assert time.monotonic()-start<test_timeout(1)
            assert response.status_code==200,response.get_json()
        assert wait_job(engine,job)['phase']=='stopped'
    finally:
        release.set();engine.controls[job].stop()
        with engine.controls[job].destination.condition:
            engine.controls[job].destination.condition.wait_for(lambda:not engine.controls[job].destination.running,test_timeout(3))


def test_exported_events_keep_storage_codes_without_raw_message(system):
    engine,job,control,_,_,_,_=recovery_fixture(system)
    with engine.store.db() as db:
        db.execute("INSERT INTO journal_events(job,time,category,level,message,path) VALUES(?,?,?,?,?,?)",(job,'2026-01-01','system','warning','Папка загрузки недоступна. Код Windows: 64. PRIVATE-NAME','PRIVATE-PATH'))
    data=bundle(engine,job)
    row=data['events.json'][0]
    assert row['event_code']=='storage_unavailable' and row['os_error_code']==64
    assert row['os_error_source']=='windows'
    assert 'PRIVATE' not in json.dumps(data)
    control.stop()


def test_late_probe_refreshes_spent_space_credit_before_manual_resume(system):
    from disk_downloader.engine import HARD_FREE_BYTES
    engine,job,control,_,identity,usage,_=recovery_fixture(system)
    entered,release=threading.Event(),threading.Event()
    def probe():
        entered.set();assert release.wait(test_timeout(3))
        return identity[0],usage[0]
    control.destination.probe=probe
    control.free_credit=HARD_FREE_BYTES-1
    control.space_usage_generation=control.destination.generation
    control.floor_latched=True
    control.pause(source='space')
    try:
        assert engine.check_space(job,control,force=True,interactive=True)
        assert entered.is_set()
        release.set()
        with control.destination.condition:
            assert control.destination.condition.wait_for(lambda:not control.destination.running,test_timeout(3))
        assert engine.check_space(job,control,force=True,interactive=True) is False
        assert control.free_credit==usage[0].free and not control.floor_latched
        assert control.space_usage_generation==control.destination.generation
    finally:release.set();stop_recovery(control)
