import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import make_job,run_job,wait_job
from disk_downloader.engine import Control
from disk_downloader.recovery import recover_publications
from disk_downloader.service_outputs import root_key,digest_file


def test_recover_service_output_retains_values_not_column_names(system):
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job=run_job(system,mode='fallback')
    relative=engine.store.job(job['id'])['settings']['journal_local']
    expected=(root_key(job['root']),'journal',relative,digest_file(Path(job['root'])/relative))
    with engine.store.db() as db:
        db.execute('DELETE FROM service_outputs WHERE root=? AND kind=?',expected[:2])
        db.execute('INSERT INTO pending_service_outputs VALUES(?,?,?,?)',expected)
    recover_publications(engine.store)
    with engine.store.db() as db:
        row=db.execute('SELECT root,kind,local,sha256 FROM service_outputs WHERE root=? AND kind=?',expected[:2]).fetchone()
        assert row and tuple(row)==expected
        assert not db.execute('SELECT 1 FROM service_outputs WHERE root=?',('root',)).fetchone()


def test_preview_internal_identity_cannot_claim_real_cloud_filename(system):
    engine,api,_=system
    api.file('disk:/photo.jpg')
    api.file('disk:/photo.jpg@preview',b'ordinary file',preview=False,media_type='document',mime_type='application/octet-stream')
    job_id=make_job(system)
    settings=engine.store.job(job_id)['settings'];settings['workers']=1
    engine.store.update_job(job_id,settings=json.dumps(settings))
    engine.action(job_id,'start');job=wait_job(engine,job_id)
    assert job['phase']=='completed'
    assert (Path(job['root'])/'photo.jpg@preview').read_bytes()==b'ordinary file'
    assert (Path(job['root'])/'photo.jpg.preview.png').is_file()


def test_many_existing_versions_do_not_grow_filename_forever(system):
    engine,api,_=system
    api.file('disk:/file.bin',b'new',preview=False)
    job_id=make_job(system,mode='fallback')
    job=engine.store.job(job_id)
    entry=next(engine.store.rows("SELECT * FROM entries WHERE job=? AND kind='file'",(job_id,)))
    for index in range(23):
        engine.transfer(job,entry,'original',api.tree[entry['path']],Control())
    files=list(Path(job['root']).glob('file*.bin'))
    assert len(files)==23 and all(p.read_bytes()==b'new' for p in files)


def test_uppercase_api_checksums_adopt_existing_file_without_download(system):
    engine,api,tmp=system
    api.file('disk:/file.bin',b'saved',preview=False)
    for key in ('md5','sha256'):api.tree['disk:/file.bin'][key]=api.tree['disk:/file.bin'][key].upper()
    root=tmp/'earlier';root.mkdir();(root/'file.bin').write_bytes(b'saved')
    job_id=engine.create({'destination':str(root),'operation':'missing','paths':['disk:/'],'mode':'fallback'})
    job=wait_job(engine,job_id)
    assert job['phase']=='ready' and job['originals']==1
    engine.action(job_id,'start');job=wait_job(engine,job_id)
    assert job['originals']==1 and api.media_calls==[]


def test_report_final_hash_obeys_cancellation(system,monkeypatch):
    from disk_downloader.reports import write_reports
    import disk_downloader.service_outputs as outputs
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job_id=make_job(system,reports=['json'])
    cancelled=threading.Event();digest=outputs.digest_file
    def slow_digest(file,checkpoint=lambda:None):
        if str(file).endswith('.part'):cancelled.set()
        return digest(file,checkpoint)
    def checkpoint():
        if cancelled.is_set():raise ValueError('cancelled')
    monkeypatch.setattr(outputs,'digest_file',slow_digest)
    with pytest.raises(ValueError,match='cancelled'):write_reports(engine.store,job_id,checkpoint)
    assert not (Path(engine.store.job(job_id)['root'])/'report.json').exists()


def test_stop_during_scan_manifest_does_not_return_ready(system,monkeypatch):
    import disk_downloader.engine as module
    engine,api,tmp=system
    api.file('disk:/one.bin',preview=False)
    original=module.save_import;stopped=False
    def cancelling(store,job_id,checkpoint=lambda:None):
        nonlocal stopped
        if not stopped:
            stopped=True;engine.controls[job_id].stop()
        checkpoint()
        return original(store,job_id,checkpoint)
    monkeypatch.setattr(module,'save_import',cancelling)
    job_id=engine.create({'destination':str(tmp/'downloads'),'paths':['disk:/']})
    assert wait_job(engine,job_id)['phase']=='stopped'


def test_failed_verification_setup_does_not_lock_all_jobs(system,monkeypatch):
    from disk_downloader.maintenance import Maintenance
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job_id=make_job(system)
    original=engine.store.db;calls=0
    @contextmanager
    def failing():
        nonlocal calls
        calls+=1
        if calls==2:raise sqlite3.OperationalError('disk full during setup')
        with original() as db:yield db
    monkeypatch.setattr(engine.store,'db',failing)
    with pytest.raises(sqlite3.OperationalError):Maintenance(engine).start({'job':job_id})
    assert not engine.maintenance_busy


from disk_downloader.updates import Updates
REAL_UPDATE_START=Updates.start


def deny_thread_start(self):
    raise RuntimeError('no worker resources')


def test_folder_picker_worker_failure_is_retryable(monkeypatch):
    from disk_downloader.folder_dialog import FolderPicker
    picker=FolderPicker(chooser=lambda _: 'chosen')
    monkeypatch.setattr(threading.Thread,'start',deny_thread_start)
    try:picker.start('')
    except RuntimeError:pass
    assert not picker.snapshot()['busy'] and picker.snapshot()['error']


def test_statistics_worker_failure_does_not_leak_pending_slot(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job_id=make_job(system)
    monkeypatch.setattr(threading.Thread,'start',deny_thread_start)
    try:engine.status_cache.snapshot(engine.store.job(job_id))
    except RuntimeError:pass
    assert not engine.status_cache.pending


def test_updates_worker_failure_is_retryable(system,monkeypatch):
    updates=Updates(system[0].store)
    monkeypatch.setattr(threading.Thread,'start',deny_thread_start)
    try:REAL_UPDATE_START(updates,force=True)
    except RuntimeError:pass
    assert not updates.busy


def test_update_download_worker_failure_is_retryable(system,monkeypatch):
    from disk_downloader.updater import Installer
    from disk_downloader.backups import Backups
    engine=system[0]
    class Release:
        def snapshot(self):
            return {'available':True,'repository':'murzirius/YaDiskDownloader','asset':{'name':'release.zip','size':100,'browser_download_url':'https://github.com/murzirius/YaDiskDownloader/releases/download/v1/release.zip'}}
    installer=Installer(engine,Release(),Backups(engine.store),8766)
    monkeypatch.setattr(threading.Thread,'start',deny_thread_start)
    try:installer.download()
    except (RuntimeError,ValueError):pass
    assert installer.snapshot()['state']=='error'


@pytest.mark.parametrize('field,value',[('workers',0),('workers',True),('workers','8'),('auto_pause','false'),('max_disk_percent',float('nan')),('speed_limit_mib',-1),('paths',['disk:/../escape']),('_inherit_source',[])])
def test_invalid_saved_runtime_options_are_flagged_before_use(system,field,value):
    engine,api,_=system
    api.file('disk:/one.bin',preview=False)
    job_id=make_job(system)
    settings=engine.store.job(job_id)['settings'];settings[field]=value
    engine.store.update_job(job_id,settings=json.dumps(settings))
    assert engine.store.job(job_id)['settings'].get('_invalid') is True


def test_malformed_legacy_schema_does_not_prevent_application_start(system):
    from disk_downloader.migration import migrate
    tmp=system[2];old=tmp/'legacy';old.mkdir()
    with sqlite3.connect(old/'jobs.sqlite3') as db:
        db.executescript("CREATE TABLE jobs(id TEXT);INSERT INTO jobs VALUES('bad');CREATE TABLE entries(job TEXT);CREATE TABLE names(job TEXT);CREATE TABLE artifacts(job TEXT);")
    result=migrate(tmp/'migrated',[old])
    assert result=={'jobs':0,'failed_sources':1}


def test_migrated_deleted_history_still_preserves_journal_membership(system):
    from disk_downloader.migration import migrate
    from disk_downloader.store import Store
    engine,api,tmp=system
    api.file('disk:/one.bin',preview=False)
    job=run_job(system,mode='fallback')
    target=tmp/'migrated'
    result=migrate(target,[Path(engine.store.filename).parent]);assert result['jobs']==1
    store=Store(target/'jobs.sqlite3')
    with store.db() as db:
        row=db.execute('SELECT root FROM journal_jobs WHERE job=?',(job['id'],)).fetchone()
        assert row and row[0]==job['root']


def test_updates_database_failure_clears_busy_and_names_local_source(system,monkeypatch):
    updates=Updates(system[0].store);updates.busy=True
    def failing_settings():raise sqlite3.OperationalError('history unavailable')
    monkeypatch.setattr(updates,'settings',failing_settings)
    updates.check()
    assert not updates.busy and 'ПК' in updates.result['message']
