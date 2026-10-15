import hashlib
import io
import json
import os
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from contextlib import closing

import pytest
from conftest import FakeAPI, make_job, run_job, wait_job, test_timeout
from disk_downloader.backups import Backups
from disk_downloader.management import Management
from disk_downloader.maintenance import Maintenance
from disk_downloader.recovery import recover_publications
from disk_downloader.reports import write_reports
from disk_downloader.service_outputs import digest_file, root_key
from disk_downloader.updater import download_url, validate_archive


def test_twenty_workers_are_supported_and_bounded(system):
    engine,api,tmp=system
    settings={'destination':str(tmp/'results'),'whole_disk':True,'workers':20}
    assert engine.validate_settings(settings)['workers']==20
    with pytest.raises(ValueError): engine.validate_settings(settings|{'workers':21})
    for n in range(40): api.file('disk:/file-'+str(n)+'.bin',preview=False)
    job_id=engine.create(settings|{'mode':'fallback','create_zip':False})
    wait_job(engine,job_id);engine.action(job_id,'start')
    result=wait_job(engine,job_id)
    assert result['phase']=='completed' and result['originals']==40 and result['errors']==0


def test_reports_are_replaced_and_foreign_file_is_not_overwritten(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False)
    job=run_job(system,mode='fallback',reports=['json'])
    root=Path(job['root']);report=root/'report.json'
    assert report.is_file()
    paths=write_reports(engine.store,job['id'])
    assert paths==['report.json'] and len(list(root.glob('report*.json')))==1
    report.write_text('foreign document',encoding='utf-8')
    paths=write_reports(engine.store,job['id'])
    assert paths!=['report.json'] and report.read_text(encoding='utf-8')=='foreign document'


def test_missing_import_reuses_report_and_journal(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False)
    first=run_job(system,mode='fallback',reports=['json'])
    api.file('disk:/two.bin',preview=False)
    second=engine.create({'destination':first['root'],'operation':'missing','whole_disk':True,'mode':'fallback','reports':['json'],'create_zip':False})
    wait_job(engine,second);engine.action(second,'start');result=wait_job(engine,second)
    root=Path(first['root'])
    assert result['phase']=='completed'
    assert len(list(root.glob('report*.json')))==len(list(root.glob('log*.html')))==1
    report=json.loads((root/'report.json').read_text(encoding='utf-8'))
    assert {'disk:/one.bin','disk:/two.bin'}.issubset({row['source_path'] for row in report['entries']})


def test_backup_includes_wal_and_restore_preserves_files(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False)
    job=run_job(system,mode='fallback')
    backups=Backups(engine.store)
    with engine.store.db() as db: db.execute("INSERT INTO profiles VALUES('mine','{}')")
    name=backups.create()
    with engine.store.db() as db: db.execute("DELETE FROM profiles")
    safety=backups.restore(name)
    assert backups.path(safety).is_file()
    assert list(engine.store.rows("SELECT name FROM profiles"))[0]['name']=='mine'
    assert (Path(job['root'])/'one.bin').read_bytes()==b'original'
    assert not list(engine.store.rows('SELECT * FROM verification_cache'))


def test_invalid_backup_does_not_replace_state(system):
    engine,api,tmp=system;api.file('disk:/one.bin')
    job=run_job(system);backups=Backups(engine.store)
    with pytest.raises((ValueError,sqlite3.Error)):backups.import_file(io.BytesIO(b'not a database'))
    assert engine.store.job(job['id'])['id']==job['id']


def test_history_pages_archive_delete_leave_results(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False)
    job=run_job(system,mode='fallback');manager=Management(engine)
    with engine.store.db() as db:
        original=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        names=original.keys();sql='INSERT INTO jobs('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')'
        for n in range(35):
            row=dict(original);row['id']='extra-'+str(n);row['title']='task '+str(n)
            db.execute(sql,tuple(row[k] for k in names))
    assert manager.history()['total']==36 and manager.history()['has_more']
    assert len(manager.history(offset=30)['items'])==6
    manager.history_action(job['id'],{'action':'archive'})
    assert manager.history(archived=True)['total']==1
    with pytest.raises(ValueError):manager.history_action(job['id'],{'action':'delete'})
    manager.history_action(job['id'],{'action':'delete','confirm':job['id']})
    assert (Path(job['root'])/'one.bin').is_file()


def test_queue_waits_for_manual_connection_and_runs_sequentially(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False)
    first=make_job(system,mode='fallback');second=make_job(system,mode='fallback');manager=Management(engine)
    manager.queue_action({'action':'add','job':first});manager.queue_action({'action':'add','job':second})
    api.credentials.clear();manager.tick()
    assert not engine.threads[first].is_alive()
    api.credentials.set('fake-token-for-tests')
    manager.tick();wait_job(engine,first);manager.tick();wait_job(engine,second);manager.tick()
    assert manager.queue_snapshot()['items']==[]
    assert engine.store.job(first)['phase']==engine.store.job(second)['phase']=='completed'


def test_queue_delete_guard_and_order(system):
    engine,api,tmp=system;first=make_job(system);second=make_job(system);manager=Management(engine)
    manager.queue_action({'action':'add','job':first});manager.queue_action({'action':'add','job':second})
    manager.queue_action({'action':'reorder','jobs':[second,first]})
    assert manager.queue_snapshot()['items'][0]['job']==second
    with pytest.raises(ValueError):manager.history_action(first,{'action':'delete','confirm':first})
    with pytest.raises(ValueError):manager.queue_action({'action':'reorder','jobs':[first,first]})


def test_offline_verification_hashes_content_and_allows_cancel(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False);api.file('disk:/two.bin',preview=False)
    job=run_job(system,mode='fallback');api.credentials.clear()
    (Path(job['root'])/'one.bin').write_bytes(b'corrupt!')
    (Path(job['root'])/'two.bin').unlink()
    maintenance=Maintenance(engine);maintenance.start({'job':job['id']});maintenance.thread.join(test_timeout(15))
    result=maintenance.snapshot()
    assert not result['busy'] and result['counts']=={'damaged':1,'missing':1}
    assert not api.credentials.connected


def test_cleanup_only_registered_obsolete_temps_and_keeps_parts(system,monkeypatch):
    from disk_downloader.paths import temporary_file
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False);job=run_job(system,mode='fallback')
    root=job['root'];obsolete=Path(temporary_file(root,engine.store));obsolete.write_bytes(b'old temp')
    stranger=Path(root)/'.disk-abcdefgh.part';stranger.write_bytes(b'user file')
    row,part,sha,md5=engine.partials.prepare(job,'disk:/next.bin','original','stamp',lambda:None)
    with engine.store.db() as db:db.execute('UPDATE temporary_files SET created=0')
    maintenance=Maintenance(engine);preview=maintenance.cleanup_preview(root)
    assert [item['local'] for item in preview['items']]==[obsolete.name]
    moved=[]
    monkeypatch.setattr('disk_downloader.maintenance.recycle',lambda file:moved.append(file))
    result=maintenance.cleanup({'root':root,'items':preview['items']})
    assert result['removed']==[obsolete.name] and stranger.read_bytes()==b'user file' and Path(part).exists()


def test_cleanup_preserves_parts_referenced_only_by_portable_manifest(system):
    from disk_downloader.paths import temporary_file
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False);job=run_job(system,mode='fallback')
    file=Path(temporary_file(job['root'],engine.store,'partial'));file.write_bytes(b'portable part')
    with engine.store.db() as db:db.execute('UPDATE temporary_files SET created=0')
    manifest=Path(job['root'])/job['settings']['manifest_local']
    with sqlite3.connect(manifest) as db:
        db.execute('INSERT INTO partials VALUES(?,?,?,?,?,?,?,?)',('disk:/next.bin','original',file.name,'stamp',file.stat().st_size,hashlib.sha256(file.read_bytes()).hexdigest(),'',None))
    assert Maintenance(engine).cleanup_preview(job['root'])['items']==[]
    assert file.read_bytes()==b'portable part'


def test_before_upgrade_backups_have_bounded_retention(system,monkeypatch):
    import disk_downloader.backups as module
    engine,api,tmp=system
    for n in range(7):
        monkeypatch.setattr(module,'__version__','1.3.'+str(n))
        module.before_upgrade(Path(engine.store.filename))
    assert len(list((Path(engine.store.filename).parent/'backups').glob('before-*.sqlite3')))==5


def test_periodic_reports_do_not_change_zip_snapshot(system,monkeypatch):
    from conftest import Response
    engine,api,tmp=system;engine.journal_interval=.05
    api.file('disk:/big.bin',data=b'x'*1024,preview=False)
    job_id=make_job(system,mode='fallback',reports=['json'])
    reached,release=threading.Event(),threading.Event()
    original=zipfile.ZipFile.open
    def opened(archive,name,mode='r',*args,**kwargs):
        stream=original(archive,name,mode,*args,**kwargs)
        if mode=='w' and getattr(name,'filename',name)=='big.bin':
            write=stream.write
            def blocked(data):
                reached.set()
                while not release.wait(.01):engine.controls[job_id].check()
                return write(data)
            stream.write=blocked
        return stream
    monkeypatch.setattr(zipfile.ZipFile,'open',opened)
    engine.action(job_id,'start')
    try:
        assert reached.wait(10)
        report=Path(engine.store.job(job_id)['root'])/'report.json'
        before=report.stat().st_mtime_ns
        time.sleep(.3)
        assert report.stat().st_mtime_ns==before
    finally:release.set()
    assert wait_job(engine,job_id)['phase']=='completed'


def test_publication_recovery_checks_digest_and_keeps_foreign_files(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False);job_id=make_job(system,mode='fallback');job=engine.store.job(job_id)
    data=b'original';target=Path(job['root'])/'one.bin';target.write_bytes(data)
    with engine.store.db() as db:db.execute('INSERT INTO pending_publications VALUES(?,?,?,?,?,?,?,?,?)',(job_id,'disk:/one.bin','original','.disk-abcdefgh.part','one.bin',len(data),hashlib.sha256(data).hexdigest(),'stamp',None))
    assert recover_publications(engine.store)==1
    assert list(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(job_id,)))[0]['local']=='one.bin'
    assert target.read_bytes()==data


def test_updater_rejects_untrusted_urls_and_unsafe_zip(tmp_path):
    download_url('https://github.com/murzirius/YaDiskDownloader/releases/download/v1.3.1/file.zip','murzirius/YaDiskDownloader',True)
    for url in ('http://github.com/file','https://github.com.evil/file','https://evil.invalid/file','https://user:pass@github.com/file'):
        with pytest.raises(ValueError):download_url(url,'murzirius/YaDiskDownloader',True)
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(bad,'w') as archive:archive.writestr('../YaDiskDownloader.exe',b'MZ'+b'x'*2000)
    with pytest.raises(ValueError):validate_archive(bad,tmp_path/'candidate.exe')


def test_updater_extracts_only_single_exe(tmp_path):
    archive_path=tmp_path/'good.zip';data=b'MZ'+b'x'*2000
    with zipfile.ZipFile(archive_path,'w') as archive:
        archive.writestr('YaDiskDownloader/YaDiskDownloader.exe',data)
        archive.writestr('YaDiskDownloader/README.md','documentation')
    candidate=tmp_path/'candidate.exe';validate_archive(archive_path,candidate)
    assert candidate.read_bytes()==data and not (tmp_path/'README.md').exists()


@pytest.mark.parametrize('bad_digest',[False,True])
def test_update_download_requires_exact_digest(system,monkeypatch,bad_digest):
    from disk_downloader.updater import Installer
    from disk_downloader.updates import Updates
    engine,api,tmp=system
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w') as archive:archive.writestr('YaDiskDownloader/YaDiskDownloader.exe',b'MZ'+b'x'*2000)
    content=payload.getvalue()
    class Response:
        status_code=200;headers={};raw=None
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def close(self):pass
        def iter_content(self,size):yield content
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def mount(self,*args):pass
        def get(self,*args,**kwargs):return Response()
    monkeypatch.setattr('disk_downloader.updater.requests.Session',Session)
    installer=Installer(engine,Updates(engine.store),Backups(engine.store),8766)
    installer.result={'state':'downloading','bytes':0}
    asset={'name':'YaDiskDownloader-v1.3.1-Windows-x64.zip','size':len(content),'digest':'sha256:'+('0'*64 if bad_digest else hashlib.sha256(content).hexdigest()),'browser_download_url':'https://github.com/murzirius/YaDiskDownloader/releases/download/v1.3.1/YaDiskDownloader-v1.3.1-Windows-x64.zip'}
    installer.fetch({'repository':'murzirius/YaDiskDownloader','latest':'v1.3.1'},asset)
    assert installer.snapshot()['state']==('error' if bad_digest else 'ready')
    assert not engine.shutting_down
