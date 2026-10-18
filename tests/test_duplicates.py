import hashlib,json,sqlite3,zipfile
from pathlib import Path

import pytest

from conftest import wait_job,run_job
from disk_downloader.engine import Control
from disk_downloader import duplicates
from disk_downloader.import_state import find_import,inherit,save_import
from disk_downloader.store import Store


def start(system,**settings):
    engine,_,tmp=system
    job=engine.create({'destination':str(tmp/'downloads'),'whole_disk':True,'mode':'fallback','create_zip':False,'reports':['json'],'workers':20,**settings})
    assert wait_job(engine,job)['phase']=='ready'
    engine.action(job,'start')
    return wait_job(engine,job)


def clean_review(engine,job_id,items=None):
    from disk_downloader.duplicate_review import DuplicateReview
    review=DuplicateReview(engine)
    records=review.snapshot(job_id)['items'] if items is None else items
    assert records
    review.start(job_id,{'confirmation':'recycle-selected','items':records})
    review.thread.join(30)
    assert not review.thread.is_alive()
    assert not review.snapshot(job_id)['cleanup']['error']
    return engine.store.summary(job_id)


def test_duplicate_settings_default_off(system):
    engine,_,tmp=system
    settings=engine.validate_settings({'destination':str(tmp),'whole_disk':True})
    assert settings['deduplicate'] is False and 'duplicate_cleanup' not in settings
    for key in ('deduplicate',):
        with pytest.raises(ValueError):engine.validate_settings({'destination':str(tmp),'whole_disk':True,key:'yes'})


def test_same_name_size_date_does_not_discard_distinct_content(system):
    engine,api,_=system
    api.directory('disk:/folder')
    api.file('disk:/same.bin',b'first',preview=False)
    api.file('disk:/folder/same.bin',b'other',preview=False)
    result=start(system,deduplicate=True,duplicate_cleanup=True)
    assert result['phase']=='completed' and result['originals']==2 and result['duplicates']==0
    rows=list(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(result['id'],)))
    assert {Path(result['root'],row['local']).read_bytes() for row in rows}=={b'first',b'other'}


def test_twenty_workers_download_identical_original_once(system):
    engine,api,_=system
    for i in range(24):api.file(f'disk:/copy-{i:02d}.bin',b'identical bytes',preview=False)
    result=start(system,deduplicate=True,create_zip=True)
    assert result['phase']=='completed',result
    assert len([c for c in api.media_calls if c[1]=='original'])==1
    assert result['originals']==24 and result['duplicates']==23 and result['saved_bytes']==15
    rows=list(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(result['id'],)))
    assert len({r['local'] for r in rows})==1
    with zipfile.ZipFile(Path(result['root'])/result['zip_path']) as archive:
        names=archive.namelist();assert len(names)==len(set(names))
        assert sum(archive.read(n)==b'identical bytes' for n in names)==1


def test_defaults_preserve_independent_copies(system):
    engine,api,_=system
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    result=start(system)
    assert result['duplicates']==0 and len(api.media_calls)==2 and result['saved_bytes']==18


def test_corrupt_canonical_is_not_reused(system):
    engine,api,_=system;api.file('disk:/first.bin',b'identical',preview=False)
    first=start(system,deduplicate=True);root=Path(first['root'])
    (root/'first.bin').write_bytes(b'corrupted')
    api.file('disk:/second.bin',b'identical',preview=False);api.media_calls.clear()
    result=start(system,operation='missing',destination=str(root),deduplicate=True)
    assert result['phase']=='completed' and result['originals']==2
    rows=list(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(result['id'],)))
    assert all((root/r['local']).read_bytes()==b'identical' for r in rows)
    assert len(api.media_calls)==1


def test_alias_survives_missing_download_without_network_transfer(system):
    engine,api,_=system
    for i in range(3):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system,deduplicate=True);api.media_calls.clear()
    result=start(system,operation='missing',destination=first['root'],deduplicate=True)
    assert result['phase']=='completed' and result['originals']==3 and not api.media_calls
    assert result['saved_bytes']==9 and result['duplicates']==2


def test_identical_previews_do_not_hide_distinct_originals(system):
    engine,api,_=system
    api.file('disk:/a.jpg',b'original one');api.file('disk:/b.jpg',b'original two')
    result=start(system,mode='both',deduplicate=True)
    assert result['originals']==2 and result['previews']==2 and result['duplicates']==0
    assert len(api.media_calls)==4


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows Recycle Bin integration')
def test_existing_owned_duplicates_move_to_recycle_and_keep_aliases(system,monkeypatch):
    engine,api,_=system
    for i in range(3):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system);removed=[]
    def recycle(path):removed.append(Path(path).read_bytes());Path(path).unlink()
    monkeypatch.setattr('disk_downloader.maintenance.recycle',recycle)
    result=start(system,operation='missing',destination=first['root'],deduplicate=True,duplicate_cleanup=True)
    assert result['phase']=='completed',result
    assert removed==[] and result['duplicates']==0
    result=clean_review(engine,result['id'])
    assert removed==[b'identical',b'identical']
    assert result['duplicates']==2 and result['saved_bytes']==9
    assert len(list(Path(first['root']).glob('copy*.bin')))==1


def test_unavailable_recycle_preserves_file_and_reference(system,monkeypatch):
    engine,api,_=system
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system)
    def fail(path):raise OSError('Recycle unavailable')
    monkeypatch.setattr('disk_downloader.maintenance.recycle',fail)
    result=start(system,operation='missing',destination=first['root'],deduplicate=True,duplicate_cleanup=True)
    assert result['phase']=='completed' and result['duplicates']==0
    result=clean_review(engine,result['id'])
    assert result['duplicates']==0
    assert len(list(Path(first['root']).glob('copy*.bin')))==2
    assert not list(Path(first['root']).glob('.yadisk-duplicate-*'))


def test_modified_duplicate_is_preserved(system,monkeypatch):
    engine,api,_=system
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system);root=Path(first['root']);(root/'copy1.bin').write_bytes(b'user edit')
    removed=[];monkeypatch.setattr('disk_downloader.maintenance.recycle',lambda p:removed.append(p))
    result=start(system,operation='missing',destination=str(root),deduplicate=True,duplicate_cleanup=True)
    assert (root/'copy1.bin').read_bytes()==b'user edit' and not removed
    assert result['originals']==2


def test_legacy_unique_schema_migrates_without_losing_rows(tmp_path):
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE artifacts(job TEXT,path TEXT,type TEXT,local TEXT,size INTEGER,sha256 TEXT,fingerprint TEXT,preview_size TEXT,PRIMARY KEY(job,path,type),UNIQUE(job,local))')
        db.execute("INSERT INTO artifacts VALUES('j','disk:/a','original','a',3,'sha','fp',NULL)")
    store=Store(path)
    with store.db() as db:
        db.execute("INSERT INTO artifacts VALUES('j','disk:/b','original','a',3,'sha','fp2',NULL)")
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]==2
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'

def test_duplicate_recovery_restores_quarantined_file(system):
    from disk_downloader.recovery import recover_publications
    engine,api,_=system;api.file('disk:/one.bin',b'kept',preview=False)
    first=start(system);root=Path(first['root']);name='.yadisk-duplicate-'+'a'*32
    (root/'one.bin').rename(root/name)
    with engine.store.db() as db:
        db.execute('INSERT INTO duplicate_moves VALUES(?,?,?,?,?)',(first['id'],'one.bin',name,hashlib.sha256(b'kept').hexdigest(),4))
    assert recover_publications(engine.store)==1
    assert (root/'one.bin').read_bytes()==b'kept' and not (root/name).exists()
    with engine.store.db() as db:assert db.execute('SELECT COUNT(*) FROM duplicate_moves').fetchone()[0]==0


def test_duplicate_recovery_never_overwrites_existing_file(system):
    from disk_downloader.recovery import recover_publications
    engine,api,_=system;api.file('disk:/one.bin',b'kept',preview=False)
    first=start(system);root=Path(first['root']);name='.yadisk-duplicate-'+'b'*32
    (root/name).write_bytes(b'kept');(root/'one.bin').write_bytes(b'new user content')
    with engine.store.db() as db:
        db.execute('INSERT INTO duplicate_moves VALUES(?,?,?,?,?)',(first['id'],'one.bin',name,hashlib.sha256(b'kept').hexdigest(),4))
    with pytest.raises(OSError):recover_publications(engine.store)
    assert (root/'one.bin').read_bytes()==b'new user content' and (root/name).read_bytes()==b'kept'


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows Recycle Bin integration')
def test_shared_preview_is_kept_until_last_reference_upgraded(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/a.jpg',b'original one');api.file('disk:/b.jpg',b'original two')
    monkeypatch.setattr('disk_downloader.maintenance.recycle',lambda path:Path(path).unlink())
    first=start(system,mode='both',deduplicate=True,duplicate_cleanup=True)
    assert first['previews']==2 and first['duplicates']==0
    first=clean_review(engine,first['id'])
    assert first['duplicates']==1
    root=Path(first['root'])
    result=start(system,operation='missing',destination=str(root),missing_policy='originals',deduplicate=True)
    assert result['phase']=='completed' and result['originals']==2 and result['previews']==0
    assert not list(root.glob('*.preview.png'))


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows Recycle Bin integration')
def test_real_recycle_bin_preserves_last_verified_copy(system):
    engine,api,_=system
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'temporary recycle integration test',preview=False)
    first=start(system)
    result=start(system,operation='missing',destination=first['root'],deduplicate=True,duplicate_cleanup=True)
    assert result['phase']=='completed' and result['duplicates']==0
    result=clean_review(engine,result['id'])
    assert result['duplicates']==1
    assert len(list(Path(first['root']).glob('copy*.bin')))==1
    row=next(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(result['id'],)))
    assert Path(first['root'],row['local']).read_bytes()==b'temporary recycle integration test'

def test_legacy_cleanup_flag_never_triggers_deletion(system,monkeypatch):
    engine,api,_=system
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system);calls=[]
    monkeypatch.setattr('disk_downloader.maintenance.recycle',lambda p:calls.append(p))
    result=start(system,operation='missing',destination=first['root'],deduplicate=True,duplicate_cleanup=True)
    assert calls==[] and len(list(Path(first['root']).glob('copy*.bin')))==2
    assert 'duplicate_cleanup' not in result['settings']
    from disk_downloader.duplicate_review import DuplicateReview
    review=DuplicateReview(engine)
    assert review.snapshot(result['id'])['total']==1
    with pytest.raises(ValueError):review.start(result['id'],{'items':review.snapshot(result['id'])['items']})
    assert calls==[]


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows Recycle Bin integration')
def test_manual_cleanup_only_selected_and_updates_portable_manifest(system,monkeypatch):
    from disk_downloader.duplicate_review import DuplicateReview
    engine,api,_=system
    for i in range(3):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system)
    result=start(system,operation='missing',destination=first['root'],deduplicate=True,create_zip=True)
    root=Path(result['root']);old_zip=root/result['zip_path'];review=DuplicateReview(engine)
    items=review.snapshot(result['id'])['items'];assert len(items)==2
    monkeypatch.setattr('disk_downloader.maintenance.recycle',lambda p:Path(p).unlink())
    review.start(result['id'],{'confirmation':'recycle-selected','items':items[:1]});review.thread.join(30)
    assert not review.thread.is_alive()
    status=review.snapshot(result['id']);assert status['total']==1 and status['cleanup']['removed']==1
    assert len(list(root.glob('copy*.bin')))==2
    assert old_zip.exists() and engine.store.job(result['id'])['zip_path'] is None
    with sqlite3.connect(root/result['settings']['manifest_local']) as db:
        locals={r[0] for r in db.execute('SELECT local FROM artifacts')}
    assert len(locals)==2 and all((root/local).exists() for local in locals)


def test_changed_after_review_is_not_removed(system,monkeypatch):
    from disk_downloader.duplicate_review import DuplicateReview
    engine,api,_=system
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system);result=start(system,operation='missing',destination=first['root'],deduplicate=True)
    root=Path(result['root']);review=DuplicateReview(engine);items=review.snapshot(result['id'])['items']
    (root/items[0]['local']).write_bytes(b'user edit');calls=[]
    monkeypatch.setattr('disk_downloader.maintenance.recycle',lambda p:calls.append(p))
    review.start(result['id'],{'confirmation':'recycle-selected','items':items});review.thread.join(30)
    assert not calls and (root/items[0]['local']).read_bytes()==b'user edit'
    assert review.snapshot(result['id'])['cleanup']['skipped']==1


def test_manual_cleanup_endpoint_requires_csrf_confirmation_and_fresh_selection(tmp_path):
    from test_api_security import web_session
    app,client,base,headers=web_session(tmp_path)
    engine=app.extensions['engine'];api=engine.api
    for i in range(2):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    system=(engine,api,tmp_path);first=start(system)
    result=start(system,operation='missing',destination=first['root'],deduplicate=True)
    url='/api/jobs/'+result['id']+'/duplicates'
    assert client.get(url,base_url=base).status_code==403
    assert client.post(url,base_url=base,json={}).status_code==403
    response=client.get(url,base_url=base,headers=headers)
    assert response.status_code==200 and response.json['total']==1
    items=response.json['items']
    for payload in ({'items':items},{'confirmation':'recycle-selected','items':[]},{'confirmation':'recycle-selected','items':[dict(items[0],canonical='wrong')]}):
        assert client.post(url,base_url=base,headers=headers,json=payload).status_code==400
    assert all(Path(result['root'],f'copy{i}.bin').exists() for i in range(2))

@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows Recycle Bin integration')
def test_manual_cleanup_cancellation_and_history_lock(system,monkeypatch):
    import threading
    from disk_downloader.duplicate_review import DuplicateReview
    from disk_downloader.management import Management
    engine,api,_=system
    for i in range(3):api.file(f'disk:/copy{i}.bin',b'identical',preview=False)
    first=start(system);result=start(system,operation='missing',destination=first['root'],deduplicate=True)
    review=DuplicateReview(engine);items=review.snapshot(result['id'])['items'];entered=threading.Event();release=threading.Event()
    def recycle(path):
        entered.set();assert release.wait(10);Path(path).unlink()
    monkeypatch.setattr('disk_downloader.maintenance.recycle',recycle)
    review.start(result['id'],{'confirmation':'recycle-selected','items':items})
    try:
        assert entered.wait(10)
        with pytest.raises(ValueError):Management(engine).history_action(result['id'],{'action':'delete','confirm':result['id']})
        review.cancel()
    finally:
        release.set();review.thread.join(30)
    assert not review.thread.is_alive() and not engine.maintenance_busy
    status=review.snapshot(result['id'])['cleanup']
    assert status['cancelled'] and status['removed']==1
    assert len(list(Path(result['root']).glob('copy*.bin')))==2
