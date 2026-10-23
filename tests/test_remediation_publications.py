"""Publication ownership, interrupted link recovery, and history isolation."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from conftest import make_job, run_job, wait_job
from disk_downloader.engine import fingerprint
from disk_downloader.import_state import save_import
from disk_downloader.management import Management
from disk_downloader.paths import safe_join, temporary_file
from disk_downloader.recovery import Recovery, recover_publications
from disk_downloader.service_outputs import root_key


def pending_copy(system, *, linked=True):
    engine,api,_ = system
    content = b'confirmed original publication'
    api.file('disk:/photo.bin',content,preview=False)
    key = make_job(system,mode='fallback',reports=['json'])
    job = engine.store.job(key)
    with engine.store.db() as db:
        entry = dict(db.execute("SELECT * FROM entries WHERE job=? AND kind='file'",(key,)).fetchone())
    stamp = fingerprint(json.loads(entry['meta']))
    _,temp,_,_ = engine.partials.prepare(job,entry['path'],'original',stamp+':original',lambda:None)
    Path(temp).write_bytes(content)
    sha = hashlib.sha256(content)
    engine.partials.save(job,entry['path'],'original',len(content),sha,'',len(content))
    target = Path(safe_join(job['root'],entry['local']))
    if linked:os.link(temp,target)
    else:os.replace(temp,target)
    with engine.store.db() as db:
        db.execute('INSERT INTO pending_publications VALUES(?,?,?,?,?,?,?,?,?)',
                   (key,entry['path'],'original',Path(temp).name,entry['local'],len(content),sha.hexdigest(),stamp,None))
    return engine,job,Path(temp),target,content


def test_manifest_is_registered_and_unchanged_file_keeps_its_name(system):
    engine,api,_ = system
    api.file('disk:/one.bin',preview=False)
    job = run_job(system,mode='fallback',reports=['json'])
    before = engine.store.job(job['id'])['settings']['manifest_local']
    save_import(engine.store,job['id'])
    current = engine.store.job(job['id'])
    assert current['settings']['manifest_local'] == before
    with engine.store.db() as db:
        output = db.execute("SELECT local,sha256 FROM service_outputs WHERE root=? AND kind='manifest'",(root_key(job['root']),)).fetchone()
        signature = db.execute("SELECT signature FROM service_output_signatures WHERE root=? AND kind='manifest'",(root_key(job['root']),)).fetchone()
    assert output is not None and output['local'] == before and signature is not None
    assert hashlib.sha256(Path(safe_join(job['root'],before)).read_bytes()).hexdigest() == output['sha256']


@pytest.mark.parametrize('condition',['edited','legacy','hardlink','foreign'])
def test_manifest_never_replaces_edited_legacy_or_linked_inventory(system, condition):
    engine,api,tmp = system
    api.file('disk:/one.bin',preview=False)
    job = run_job(system,mode='fallback',reports=['json'])
    original = Path(safe_join(job['root'],engine.store.job(job['id'])['settings']['manifest_local']))
    if condition == 'edited':
        with closing(sqlite3.connect(original)) as db,db:
            db.execute('CREATE TABLE local_annotation(value TEXT)')
            db.execute("INSERT INTO local_annotation VALUES('preserve this inventory')")
    elif condition == 'legacy':
        with engine.store.db() as db:
            db.execute("DELETE FROM service_outputs WHERE kind='manifest'")
            db.execute("DELETE FROM service_output_signatures WHERE kind='manifest'")
    elif condition == 'hardlink':
        os.link(original,tmp/'second-inventory.sqlite3')
    else:
        replacement = tmp/'foreign-inventory.sqlite3'
        with closing(sqlite3.connect(replacement)) as db,db:
            db.execute('CREATE TABLE import_meta(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO import_meta VALUES('format','disk-local-import-v1')")
            db.execute('CREATE TABLE other_application_data(value TEXT)')
            db.execute("INSERT INTO other_application_data VALUES('preserve this file')")
        os.replace(replacement,original)
    before = original.read_bytes()
    save_import(engine.store,job['id'])
    after = engine.store.job(job['id'])['settings']['manifest_local']
    assert after != original.name
    assert original.read_bytes() == before
    if condition == 'hardlink':assert (tmp/'second-inventory.sqlite3').read_bytes() == before


def test_owned_two_name_publication_recovers_without_downloading_again(system):
    engine,job,temp,target,content = pending_copy(system)
    assert target.stat().st_nlink == 2
    assert recover_publications(engine.store) == 1
    assert not temp.exists() and target.read_bytes() == content and target.stat().st_nlink == 1
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pending_publications WHERE job=?',(job['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM partials WHERE job=?',(job['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM temporary_files WHERE root=? AND local=?',(root_key(job['root']),temp.name)).fetchone()[0] == 0
    engine.api.media_calls.clear()
    engine.action(job['id'],'start')
    completed = wait_job(engine,job['id'])
    assert completed['phase'] == 'completed' and completed['originals'] == 1
    assert engine.api.media_calls == []
    assert [file.name for file in Path(job['root']).glob('photo*.bin')] == [target.name]


@pytest.mark.parametrize('condition',['no_owner','no_registry','wrong_purpose','no_partial','wrong_stamp','wrong_hash','third_link'])
def test_ambiguous_or_unowned_link_pair_is_preserved(system, condition):
    engine,job,temp,target,content = pending_copy(system)
    with engine.store.db() as db:
        if condition == 'no_owner':db.execute('DELETE FROM partial_ownership')
        elif condition == 'no_registry':db.execute('DELETE FROM temporary_files WHERE local=?',(temp.name,))
        elif condition == 'wrong_purpose':db.execute("UPDATE temporary_files SET purpose='report' WHERE local=?",(temp.name,))
        elif condition == 'no_partial':db.execute('DELETE FROM partials')
        elif condition == 'wrong_stamp':db.execute("UPDATE partials SET fingerprint='different'")
        elif condition == 'wrong_hash':db.execute("UPDATE pending_publications SET sha256=?",('0'*64,))
    if condition == 'third_link':os.link(temp,Path(job['root'])/'third-link.bin')
    assert recover_publications(engine.store) == 0
    assert temp.read_bytes() == target.read_bytes() == content
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM pending_publications WHERE job=?',(job['id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('purpose,kind',[('manifest','manifest'),('report','report:json'),('journal','journal'),('changes','changes')])
def test_service_output_recovers_only_its_registered_two_name_pair(tmp_path, purpose, kind):
    from disk_downloader.store import Store
    store = Store(tmp_path/'state/jobs.sqlite3')
    root = tmp_path/'files'
    root.mkdir()
    temp = Path(temporary_file(root,store,purpose))
    content = b'verified service output'
    temp.write_bytes(content)
    target = root/'service-output.bin'
    os.link(temp,target)
    with store.db() as db:
        db.execute('INSERT INTO pending_service_outputs VALUES(?,?,?,?)',(root_key(root),kind,target.name,hashlib.sha256(content).hexdigest()))
    recover_publications(store)
    assert not temp.exists() and target.read_bytes() == content and target.stat().st_nlink == 1
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pending_service_outputs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM service_outputs WHERE kind=?',(kind,)).fetchone()[0] == 1


@pytest.mark.parametrize('action',['delete','rename','archive'])
def test_history_mutation_waits_until_background_recovery_is_complete(system, monkeypatch, action):
    import disk_downloader.recovery as module
    engine,job,temp,target,content = pending_copy(system,linked=False)
    recovery = Recovery(engine.store)
    engine.recovery = recovery
    entered,release = threading.Event(),threading.Event()
    original = module.digest_file
    def digest(path,checkpoint=lambda:None):
        entered.set()
        assert release.wait(5)
        return original(path,checkpoint)
    monkeypatch.setattr(module,'digest_file',digest)
    recovery.start()
    try:
        assert entered.wait(3)
        with pytest.raises(ValueError,match='восстана'):
            Management(engine).history_action(job['id'],{'action':action,'confirm':job['id'],'title':'renamed'})
    finally:
        release.set()
        recovery.thread.join(5)
    assert not recovery.thread.is_alive()
    assert engine.store.job(job['id'])['title'] == job['title']
    assert target.read_bytes() == content


def test_direct_recovery_does_not_commit_an_orphan_after_history_deletion(system, monkeypatch):
    import disk_downloader.recovery as module
    engine,job,temp,target,content = pending_copy(system,linked=False)
    entered,release = threading.Event(),threading.Event()
    original = module.digest_file
    results = []
    def digest(path,checkpoint=lambda:None):
        entered.set()
        assert release.wait(5)
        return original(path,checkpoint)
    monkeypatch.setattr(module,'digest_file',digest)
    worker = threading.Thread(target=lambda:results.append(recover_publications(engine.store)),daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        Management(engine).history_action(job['id'],{'action':'delete','confirm':job['id']})
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and results == [0]
    assert target.read_bytes() == content
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job['id'],)).fetchone()[0] == 0


def test_wrong_inode_pair_does_not_remove_any_copy(system):
    engine,job,temp,target,content = pending_copy(system)
    temp.unlink()
    alias = Path(job['root'])/'unrelated-alias.bin'
    os.link(target,alias)
    temp.write_bytes(content)
    assert target.stat().st_ino != temp.stat().st_ino
    assert recover_publications(engine.store) == 0
    assert temp.read_bytes() == target.read_bytes() == alias.read_bytes() == content


def test_unowned_media_pair_does_not_hash_content(system, monkeypatch):
    import disk_downloader.recovery as module
    engine,job,temp,target,content = pending_copy(system)
    with engine.store.db() as db:db.execute('DELETE FROM partial_ownership')
    calls = []
    original = module.digest_file
    def digest(*args,**kwargs):
        calls.append(args[0])
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'digest_file',digest)
    assert recover_publications(engine.store) == 0
    assert calls == [],'An unowned link pair caused a full content read before admission'
    assert temp.read_bytes() == target.read_bytes() == content


@pytest.mark.parametrize('condition',['no_registry','wrong_purpose','unknown_kind'])
def test_unowned_service_pair_does_not_hash_content(tmp_path, monkeypatch, condition):
    import disk_downloader.recovery as module
    from disk_downloader.store import Store
    store = Store(tmp_path/'state/jobs.sqlite3')
    root = tmp_path/'files'
    root.mkdir()
    temp = Path(temporary_file(root,store,'manifest'))
    content = b'preserved unowned service output'
    temp.write_bytes(content)
    target = root/'service.bin'
    os.link(temp,target)
    with store.db() as db:
        db.execute('INSERT INTO pending_service_outputs VALUES(?,?,?,?)',
                   (root_key(root),'unknown' if condition == 'unknown_kind' else 'manifest',target.name,hashlib.sha256(content).hexdigest()))
        if condition == 'no_registry':db.execute('DELETE FROM temporary_files')
        elif condition == 'wrong_purpose':db.execute("UPDATE temporary_files SET purpose='partial'")
    calls = []
    original = module.digest_file
    def digest(*args,**kwargs):
        calls.append(args[0])
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'digest_file',digest)
    recover_publications(store)
    assert calls == [],'An unproven service pair caused a full content read before admission'
    assert temp.read_bytes() == target.read_bytes() == content


def test_pair_changed_during_hash_is_not_unlinked_or_registered(system, monkeypatch):
    import disk_downloader.recovery as module
    engine,job,temp,target,content = pending_copy(system)
    original = module.digest_file
    foreign = b'foreign replacement data'
    def digest(path,checkpoint=lambda:None):
        result = original(path,checkpoint)
        if os.path.normcase(str(path)) == os.path.normcase(str(target)):
            target.unlink()
            target.write_bytes(foreign)
        return result
    monkeypatch.setattr(module,'digest_file',digest)
    assert recover_publications(engine.store) == 0
    assert temp.read_bytes() == content and target.read_bytes() == foreign
    with engine.store.db() as db:assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job['id'],)).fetchone()[0] == 0


def test_cancellation_during_pair_hash_preserves_both_names(system):
    engine,job,temp,target,content = pending_copy(system)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 2:raise InterruptedError('synthetic stop during content verification')
    with pytest.raises(InterruptedError):recover_publications(engine.store,checkpoint=cancel)
    assert temp.read_bytes() == target.read_bytes() == content and target.stat().st_nlink == 2


def test_cancel_after_owned_unlink_leaves_recoverable_intent(system, monkeypatch):
    import disk_downloader.recovery as module
    engine,job,temp,target,content = pending_copy(system)
    removed = threading.Event()
    original = os.unlink
    def unlink(path,*args,**kwargs):
        result = original(path,*args,**kwargs)
        if os.path.normcase(str(path)) == os.path.normcase(str(temp)):removed.set()
        return result
    monkeypatch.setattr(module.os,'unlink',unlink)
    def cancel():
        if removed.is_set():raise InterruptedError('synthetic stop before artifact transaction')
    with pytest.raises(InterruptedError):recover_publications(engine.store,checkpoint=cancel)
    assert not temp.exists() and target.read_bytes() == content
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pending_publications WHERE job=?',(job['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job['id'],)).fetchone()[0] == 0
    assert recover_publications(engine.store) == 1
    assert target.read_bytes() == content
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM temporary_files WHERE root=? AND local=?',(root_key(job['root']),temp.name)).fetchone()[0] == 0


def test_pending_changed_while_hashing_prevents_pair_cleanup(system, monkeypatch):
    import disk_downloader.recovery as module
    engine,job,temp,target,content = pending_copy(system)
    original = module.digest_file
    def digest(path,checkpoint=lambda:None):
        result = original(path,checkpoint)
        with engine.store.db() as db:db.execute("UPDATE pending_publications SET fingerprint='new publication'")
        return result
    monkeypatch.setattr(module,'digest_file',digest)
    assert recover_publications(engine.store) == 0
    assert temp.read_bytes() == target.read_bytes() == content and target.stat().st_nlink == 2


@pytest.mark.parametrize('condition',['no_registry','wrong_purpose','unknown_kind','wrong_hash','third_link'])
def test_unproven_service_output_pairs_preserve_every_name(tmp_path, condition):
    from disk_downloader.store import Store
    store = Store(tmp_path/'state/jobs.sqlite3')
    root = tmp_path/'files'
    root.mkdir()
    temp = Path(temporary_file(root,store,'manifest'))
    content = b'preserved service output'
    temp.write_bytes(content)
    target = root/'service.bin'
    os.link(temp,target)
    with store.db() as db:
        db.execute('INSERT INTO pending_service_outputs VALUES(?,?,?,?)',
                   (root_key(root),'unknown' if condition == 'unknown_kind' else 'manifest',target.name,
                    '0'*64 if condition == 'wrong_hash' else hashlib.sha256(content).hexdigest()))
        if condition == 'no_registry':db.execute('DELETE FROM temporary_files')
        elif condition == 'wrong_purpose':db.execute("UPDATE temporary_files SET purpose='partial'")
    if condition == 'third_link':os.link(target,root/'foreign-third.bin')
    recover_publications(store)
    assert temp.read_bytes() == target.read_bytes() == content
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pending_service_outputs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM service_outputs').fetchone()[0] == 0


@pytest.mark.skipif(os.name != 'nt',reason='Case aliases refer to one NTFS name')
def test_service_output_ambiguous_registered_alias_is_preserved(tmp_path):
    from disk_downloader.store import Store
    store = Store(tmp_path/'state/jobs.sqlite3')
    root = tmp_path/'files'
    root.mkdir()
    temp = Path(temporary_file(root,store,'manifest'))
    content = b'preserved ambiguous service output'
    temp.write_bytes(content)
    target = root/'service.bin'
    os.link(temp,target)
    with store.db() as db:
        db.execute('INSERT INTO pending_service_outputs VALUES(?,?,?,?)',(root_key(root),'manifest',target.name,hashlib.sha256(content).hexdigest()))
        db.execute('INSERT INTO temporary_files VALUES(?,?,?,?)',(root_key(root),temp.name.upper(),'manifest',0))
    recover_publications(store)
    assert temp.read_bytes() == target.read_bytes() == content and target.stat().st_nlink == 2


def test_concurrent_manifest_writes_use_one_serial_publication(system, monkeypatch):
    import disk_downloader.service_outputs as module
    engine,api,_ = system
    api.file('disk:/one.bin',preview=False)
    job = run_job(system,mode='fallback',reports=['json'])
    entered,release,second_entered = threading.Event(),threading.Event(),threading.Event()
    original = module.finish
    calls = []
    errors = []
    def finish(*args,**kwargs):
        if args[2] == 'manifest':
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            else:second_entered.set()
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'finish',finish)
    def write():
        try:save_import(engine.store,job['id'])
        except Exception as exc:errors.append(exc)
    first = threading.Thread(target=write,name='first-manifest',daemon=True)
    second = threading.Thread(target=write,name='second-manifest',daemon=True)
    first.start()
    try:
        assert entered.wait(3)
        second.start()
        assert not second_entered.wait(.2)
    finally:
        release.set()
        first.join(5)
        if second.ident:second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors,errors
    assert calls == ['first-manifest','second-manifest']
    assert len(list(Path(job['root']).glob('.disk-import*.sqlite3'))) == 1


def test_service_cancel_after_unlink_retries_without_guessing_temp_ownership(tmp_path, monkeypatch):
    import disk_downloader.recovery as module
    from disk_downloader.store import Store
    store = Store(tmp_path/'state/jobs.sqlite3')
    root = tmp_path/'files'
    root.mkdir()
    temp = Path(temporary_file(root,store,'manifest'))
    content = b'verified service output before commit'
    temp.write_bytes(content)
    target = root/'service.bin'
    os.link(temp,target)
    unrelated = '.disk-unrelated123.part'
    with store.db() as db:
        db.execute('INSERT INTO pending_service_outputs VALUES(?,?,?,?)',(root_key(root),'manifest',target.name,hashlib.sha256(content).hexdigest()))
        db.execute('INSERT INTO temporary_files VALUES(?,?,?,?)',(root_key(root),unrelated,'manifest',0))
    removed = threading.Event()
    original = os.unlink
    def unlink(path,*args,**kwargs):
        result = original(path,*args,**kwargs)
        if os.path.normcase(str(path)) == os.path.normcase(str(temp)):removed.set()
        return result
    monkeypatch.setattr(module.os,'unlink',unlink)
    def cancel():
        if removed.is_set():raise InterruptedError('synthetic stop before service output commit')
    with pytest.raises(InterruptedError):recover_publications(store,checkpoint=cancel)
    assert not temp.exists() and target.read_bytes() == content
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pending_service_outputs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM service_outputs').fetchone()[0] == 0
    recover_publications(store)
    assert target.read_bytes() == content and target.stat().st_nlink == 1
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pending_service_outputs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM service_outputs').fetchone()[0] == 1
        # No persisted pending→temporary association exists for service outputs.
        # Preserve conservative metadata rather than guess from missing files.
        assert {row[0] for row in db.execute('SELECT local FROM temporary_files')} == {temp.name,unrelated}


@pytest.mark.parametrize('kind', ['original', 'preview'])
@pytest.mark.parametrize('publication', ['native', 'forced-link'])
def test_native_posix_process_killed_between_link_and_unlink_recovers(tmp_path, kind, publication):
    if os.name == 'nt' and publication == 'native':
        pytest.skip('Requires native POSIX link/unlink crash window')
    from disk_downloader.engine import Engine
    from disk_downloader.store import Store
    from conftest import FakeAPI
    code = '''
import os,sys,time
from pathlib import Path
from conftest import FakeAPI
from disk_downloader.engine import Engine
from disk_downloader.store import Store
import disk_downloader.paths as paths
import disk_downloader.engine as engine_module
base=Path(sys.argv[1])
kind=sys.argv[2]
api=FakeAPI();api.file('disk:/photo.bin',b'original',preview=True)
store=Store(base/'state/jobs.sqlite3');engine=Engine(store,api)
original=paths.os.unlink
if sys.argv[3]=='forced-link':
    def link_publish(temp,target):
        os.link(temp,target)
        os.unlink(temp)
    engine_module.publish=link_publish
def unlink(name,*args,**kwargs):
    if Path(name).name.startswith('.disk-') and Path(name).exists() and os.stat(name).st_nlink==2:
        with store.db() as db:
            pending=db.execute('SELECT 1 FROM pending_publications WHERE temp=? AND type=?',(Path(name).name,kind)).fetchone()
        if pending:
            (base/'reached').write_text('linked',encoding='ascii')
            while True:time.sleep(.05)
    return original(name,*args,**kwargs)
paths.os.unlink=unlink
key=engine.create({'destination':str(base/'results'),'whole_disk':True,'mode':'fallback' if kind=='original' else 'previews','reports':['json'],'create_zip':False,'auto_start':True,'auto_pause':False})
(base/'job').write_text(key,encoding='ascii')
while True:time.sleep(.05)
'''
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)))
    process = subprocess.Popen([sys.executable,'-B','-c',code,str(tmp_path),kind,publication],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        due = time.monotonic()+30
        while time.monotonic()<due and not (tmp_path/'reached').is_file():
            if process.poll() is not None:raise AssertionError(process.communicate()[1].decode(errors='replace'))
            time.sleep(.05)
        assert (tmp_path/'reached').is_file()
        process.kill();process.wait(timeout=5)
        key = (tmp_path/'job').read_text(encoding='ascii')
        store = Store(tmp_path/'state/jobs.sqlite3')
        assert recover_publications(store) == 1
        api = FakeAPI();api.file('disk:/photo.bin',b'original',preview=True)
        engine = Engine(store,api)
        engine.action(key,'resume')
        assert wait_job(engine,key)['phase'] == 'completed'
        assert api.media_calls == []
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
