import json
import threading
import time
from pathlib import Path

import pytest

from conftest import run_job, make_job, wait_job, test_timeout
from disk_downloader.api import APIError
from disk_downloader.maintenance import Maintenance
from disk_downloader.management import Management
from disk_downloader.reports import write_reports
from disk_downloader.folder_usage import FolderUsage


def test_metadata_outage_keeps_verified_artifact(system, monkeypatch):
    engine, api, _ = system
    api.file('disk:/photo.jpg')
    job = run_job(system, mode='fallback')
    original = api.metadata
    def failing(path, *args, **kwargs):
        if path == 'disk:/photo.jpg':
            raise APIError('Яндекс.Диск временно недоступен', 503)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(api, 'metadata', failing)
    engine.action(job['id'], 'retry_selected', {'paths':['disk:/photo.jpg']})
    result = wait_job(engine, job['id'])
    assert result['originals'] == 1 and result['errors'] == 1
    assert (Path(job['root'])/'photo.jpg').is_file()


def test_offline_check_includes_never_downloaded_and_repairs_full_scope(system):
    engine, api, _ = system
    api.file('disk:/never.bin', preview=False)
    job_id = make_job(system)
    check = Maintenance(engine)
    check.start({'job':job_id})
    check.thread.join(test_timeout(15))
    result = check.snapshot()
    assert result['complete'] and result['counts'] == {'not_downloaded':1}
    settings = check.repair_settings()
    assert settings['paths'] == ['disk:/'] and settings['operation']=='missing'


def test_verification_pages_do_not_limit_repair(system):
    engine, api, _ = system
    api.file('disk:/one.bin', preview=False)
    job_id = make_job(system)
    with engine.store.db() as db:
        db.executemany('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',
                       [(job_id,f'disk:/missing-{i}.bin','original',f'missing-{i}.bin',1,'0'*64,'stamp',None) for i in range(2005)])
    check = Maintenance(engine)
    check.start({'job':job_id})
    check.thread.join(test_timeout(30))
    result = check.snapshot()
    assert result['complete'] and result['problems']==2006 and result['has_more']
    paths = set()
    for offset in range(0,2100,100):
        paths.update(item['path'] for item in check.snapshot(offset)['items'])
    assert len(paths)==2006
    assert check.repair_settings()['paths']==['disk:/']


@pytest.mark.parametrize('field,value', [('cancelled',True),('error','Папка недоступна'),('complete',False)])
def test_incomplete_verification_cannot_prepare_partial_repair(system,field,value):
    check = Maintenance(system[0])
    check.result.update(complete=True,problems=1,cancelled=False,error='',root='unused')
    check.result[field]=value
    with pytest.raises(ValueError, match='завершите проверку'):
        check.repair_settings()


def test_manifest_failure_is_visible(system,monkeypatch):
    engine, api, _ = system
    api.file('disk:/file.bin',preview=False)
    job_id = make_job(system,mode='fallback')
    def fail(*args): raise PermissionError('test')
    monkeypatch.setattr('disk_downloader.engine.save_import',fail)
    engine.action(job_id,'start')
    result=wait_job(engine,job_id)
    assert result['phase']=='completed_with_errors'
    assert 'опись' in result['error'] and result['originals']==1


def test_manually_completed_queued_job_is_not_restarted(system,monkeypatch):
    engine, api, _ = system
    api.file('disk:/file.bin',preview=False)
    job_id=make_job(system,mode='fallback')
    queue=Management(engine)
    queue.queue_action({'action':'add','job':job_id})
    engine.action(job_id,'start')
    wait_job(engine,job_id)
    calls=[]
    monkeypatch.setattr(engine,'action',lambda *args,**kwargs:calls.append(args))
    queue.tick()
    assert not calls and not queue.queue_snapshot()['items']


def test_history_search_uses_unicode_casefold(system):
    engine,api,_=system
    api.file('disk:/one.bin')
    job_id=make_job(system)
    with engine.store.db() as db: db.execute('UPDATE jobs SET title=? WHERE id=?',('СЕМЕЙНЫЕ ФОТО',job_id))
    assert Management(engine).history('семейные')['total']==1


def test_resolved_scan_error_is_removed_from_merged_report(system):
    engine,api,_=system
    api.file('disk:/one.bin')
    job_id=make_job(system)
    with engine.store.db() as db: db.execute('INSERT INTO scan_errors VALUES(?,?,?)',(job_id,'disk:/','temporary'))
    write_reports(engine.store,job_id)
    with engine.store.db() as db: db.execute('DELETE FROM scan_errors WHERE job=?',(job_id,))
    write_reports(engine.store,job_id)
    with engine.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM report_records WHERE kind='scan_error'").fetchone()[0]==0


def test_unavailable_folder_is_not_reported_as_empty(tmp_path):
    usage=FolderUsage()
    root=str(tmp_path/'missing')
    usage.folders[root]={'bytes':None,'updated':None,'scanning':True,'due':0,'checked':0,'error':''}
    usage.scan(root)
    assert usage.folders[root]['bytes'] is None and usage.folders[root]['error']


def test_inheritance_runs_outside_request_and_can_stop_resume(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/kept.bin',b'keep',preview=False)
    first=run_job(system,mode='fallback')
    entered=threading.Event()
    from disk_downloader.engine import inherit
    def blocked(store,job_id,previous,checkpoint,progress):
        entered.set()
        while True:
            checkpoint()
            time.sleep(.01)
    monkeypatch.setattr('disk_downloader.engine.inherit',blocked)
    started=time.monotonic()
    job_id=engine.create({'destination':first['root'],'operation':'missing'})
    assert time.monotonic()-started<test_timeout(2)
    assert entered.wait(test_timeout(5))
    assert engine.lock.acquire(blocking=False)
    engine.lock.release()
    engine.action(job_id,'stop')
    stopped=wait_job(engine,job_id)
    assert stopped['phase']=='stopped' and stopped['settings']['_inherit_source']
    monkeypatch.setattr('disk_downloader.engine.inherit',inherit)
    engine.action(job_id,'continue_scan')
    ready=wait_job(engine,job_id)
    assert ready['phase']=='ready' and ready['originals']==1
    assert not ready['settings'].get('_inherit_source')
    assert (Path(first['root'])/'kept.bin').read_bytes()==b'keep'


def test_cached_check_uses_bounded_database_work(system,monkeypatch):
    from disk_downloader.engine import Control
    engine,api,_=system
    for index in range(20):api.file(f'disk:/{index}.bin',b'data',preview=False)
    first=run_job(system,mode='fallback')
    job=engine.store.job(first['id']);job['settings']['operation']='missing'
    count=0;connect=engine.store._connect
    def measured():
        nonlocal count
        count+=1
        return connect()
    monkeypatch.setattr(engine.store,'_connect',measured)
    engine.check_local_results(job,Control())
    assert count<=4
    assert engine.store.summary(first['id'])['originals']==20


def test_local_exports_work_without_token_and_preserve_job_result(system):
    engine,api,_=system
    api.file('disk:/kept.bin',b'keep',preview=False)
    first=run_job(system,mode='fallback')
    api.credentials.clear()
    engine.action(first['id'],'refresh_journal')
    refreshed=wait_job(engine,first['id'])
    assert refreshed['phase']==first['phase'] and refreshed['originals']==1
    engine.action(first['id'],'save_manifest')
    saved=wait_job(engine,first['id'])
    assert saved['phase']=='completed' and not saved['error']
    assert (Path(first['root'])/'kept.bin').read_bytes()==b'keep'


def test_phase_transition_does_not_display_stale_finished_counters(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/one.bin')
    job_id=make_job(system)
    job=engine.store.job(job_id)
    cached=engine.status(job_id)
    cached['phase']='preparing';cached['files']=0
    engine.status_cache.items[job_id]={'value':cached,'due':time.monotonic()+100}
    monkeypatch.setattr(engine.status_cache,'refresh',lambda *args:None)
    result=engine.status_cache.snapshot(job)
    assert result['phase']=='ready' and result['statistics_pending']


def test_refreshed_folder_size_does_not_keep_deleted_files_in_history(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/removed.bin',b'saved content',preview=False)
    job=run_job(system,mode='fallback')
    (Path(job['root'])/'removed.bin').unlink()
    monkeypatch.setattr(engine.folder_usage,'snapshot',lambda *args,**kwargs:{'bytes':0,'updated':time.time(),'scanning':False})
    status=engine.status(job['id'])
    assert status['saved_bytes']>0 and status['folder_bytes']==0


def test_completed_job_requests_final_folder_size(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/one.bin',b'content',preview=False)
    forced=threading.Event()
    def snapshot(root,force=False,**kwargs):
        if force: forced.set()
        return {'bytes':0,'updated':time.time(),'scanning':False}
    monkeypatch.setattr(engine.folder_usage,'snapshot',snapshot)
    job_id=make_job(system,mode='fallback')
    forced.clear()
    engine.action(job_id,'start')
    result=wait_job(engine,job_id)
    assert result['phase']=='completed' and forced.is_set()


def test_forced_folder_refresh_survives_busy_worker_limit(tmp_path):
    usage=FolderUsage()
    root=str(tmp_path)
    (tmp_path/'file.bin').write_bytes(b'abc')
    for name in ('busy-one','busy-two'):
        usage.folders[name]={'bytes':None,'updated':None,'checked':time.monotonic(),'due':0,'error':'','scanning':True}
    assert usage.snapshot(root,force=True)['scanning']
    assert usage.folders[root]['dirty'] and not usage.folders[root]['scanning']
    usage.folders['busy-one']['scanning']=False
    usage.snapshot(root)
    due=time.monotonic()+test_timeout(5)
    while usage.folders[root]['scanning'] and time.monotonic()<due:time.sleep(.01)
    assert not usage.folders[root]['scanning'] and not usage.folders[root]['dirty']
    assert usage.folders[root]['bytes']==3


def test_manifest_cancelled_after_commit_does_not_replace_previous(system,monkeypatch):
    import sqlite3
    import disk_downloader.import_state as module
    engine,api,_=system
    api.file('disk:/kept.bin',b'keep',preview=False)
    job=run_job(system,mode='fallback')
    manifest=Path(job['root'])/engine.store.job(job['id'])['settings']['manifest_local']
    before=manifest.read_bytes()
    cancelled=threading.Event()
    connect=sqlite3.connect
    class LateCancel(sqlite3.Connection):
        def commit(self):
            super().commit()
            cancelled.set()
    def connecting(filename,*args,**kwargs):
        if str(filename).endswith('.part'):kwargs['factory']=LateCancel
        return connect(filename,*args,**kwargs)
    monkeypatch.setattr(module.sqlite3,'connect',connecting)
    def checkpoint():
        if cancelled.is_set():raise ValueError('cancelled')
    with pytest.raises(ValueError,match='cancelled'):
        module.save_import(engine.store,job['id'],checkpoint)
    assert cancelled.is_set() and manifest.read_bytes()==before


def test_journal_close_waits_for_writer_before_final_flush(system,monkeypatch):
    from disk_downloader.journal import Journal
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job=run_job(system,mode='fallback')
    journal=Journal(engine,job['id'])
    released=threading.Event()
    journal.thread=threading.Thread(target=lambda:released.wait(test_timeout(10)))
    timer=threading.Timer(3.3,released.set)
    journal.thread.start();timer.start()
    def final_flush(final=False):
        assert final and not journal.thread.is_alive()
    monkeypatch.setattr(journal,'flush',final_flush)
    try:journal.close()
    finally:
        released.set();journal.thread.join(test_timeout(10));timer.cancel()
