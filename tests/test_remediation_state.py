"""Regression coverage for damaged settings and portable comparison metadata."""
import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from conftest import FakeAPI, wait_job
from disk_downloader.engine import Engine
from disk_downloader.inventory import compare
from disk_downloader.store import Store
from disk_downloader.web import create_app


@pytest.mark.parametrize('table', ['jobs', 'profiles'])
@pytest.mark.parametrize('value', [b'\x80', b'{"broken":', b'[]', b'null', b'17',
                                 pytest.param(b'['*1200+b']'*1200,id='deep-json')])
def test_damaged_settings_are_isolated_and_original_bytes_preserved(tmp_path, table, value):
    state = tmp_path/'state'
    store = Store(state/'jobs.sqlite3')
    api = FakeAPI()
    settings = Engine(store, api).validate_settings({'destination':str(tmp_path/'files'), 'whole_disk':True})
    with store.db() as db:
        for key in ('healthy', 'damaged'):
            db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                       (key, json.dumps(settings), str(tmp_path/'files'), 'service', 'ready', '2026-01-01'))
        db.execute('INSERT INTO profiles VALUES(?,?)', ('healthy', json.dumps(settings)))
        if table == 'jobs':
            db.execute('UPDATE jobs SET settings=CAST(? AS TEXT) WHERE id=?', (value, 'damaged'))
        else:
            db.execute('INSERT INTO profiles VALUES(?,CAST(? AS TEXT))', ('damaged', value))
    reopened = Store(store.filename)
    assert not reopened.job('healthy')['settings'].get('_invalid')
    assert reopened.summary('healthy')['files'] == 0
    if table == 'jobs':
        damaged = reopened.job('damaged')
        assert damaged['settings']['_invalid'] and damaged['phase'] == 'failed'
        assert 'повреждены' in damaged['error']
    app = create_app(state, api=api)
    client = app.test_client()
    base = 'http://127.0.0.1:8766'
    with client.session_transaction(base_url=base) as session:
        session['csrf'] = 'test'
    response = client.get('/api/profiles', base_url=base, headers={'X-CSRF-Token':'test'})
    assert response.status_code == 200
    profiles = {row['name']:row for row in response.json['items']}
    assert profiles['healthy']['settings']['workers'] == settings['workers']
    if table == 'profiles':
        assert profiles['damaged']['settings'] is None and profiles['damaged']['error']
    with reopened.db() as db:
        key = 'id' if table == 'jobs' else 'name'
        assert db.execute('SELECT CAST(settings AS BLOB) FROM '+table+' WHERE '+key+'=?', ('damaged',)).fetchone()[0] == value


@pytest.mark.parametrize('source_mode', ['wal', 'delete', 'portable'])
@pytest.mark.parametrize('bad_meta', ['{"size":', '[]', 'null', '17', '"text"',
                                    pytest.param('['*1200+']'*1200,id='deep-json')])
def test_bad_baseline_json_does_not_block_verified_missing_download(tmp_path, monkeypatch, source_mode, bad_meta):
    if source_mode == 'delete':
        monkeypatch.setattr('disk_downloader.store.journal_mode', lambda:'DELETE')
    api = FakeAPI()
    api.file('disk:/bad.bin', b'preserved-original', preview=False)
    api.file('disk:/good.bin', b'healthy-neighbor', preview=False)
    engine = Engine(Store(tmp_path/'state/jobs.sqlite3'), api)
    first_id = engine.create({'destination':str(tmp_path/'files'), 'whole_disk':True, 'mode':'fallback',
                              'reports':['json'], 'create_zip':False, 'auto_start':True})
    first = wait_job(engine, first_id)
    assert first['phase'] == 'completed'
    with engine.store.db() as db:
        original_local = db.execute('SELECT local FROM artifacts WHERE job=? AND path=?', (first_id,'disk:/bad.bin')).fetchone()[0]
    original = Path(first['root'])/original_local
    if source_mode == 'portable':
        source = Path(first['root'])/first['settings']['manifest_local']
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('UPDATE inventory SET meta=? WHERE path=?', (bad_meta, 'disk:/bad.bin'))
        engine = Engine(Store(tmp_path/'fresh/jobs.sqlite3'), api)
    else:
        source = Path(engine.store.filename)
        with engine.store.db() as db:
            db.execute('UPDATE entries SET meta=? WHERE job=? AND path=?', (bad_meta, first_id, 'disk:/bad.bin'))
    api.media_calls.clear()
    key = engine.create({'destination':first['root'], 'operation':'missing', 'mode':'fallback',
                         'reports':['json'], 'create_zip':False, 'auto_start':True})
    result = wait_job(engine, key)
    assert result['phase'] == 'completed', result.get('error')
    assert result['settings']['history_warning']
    assert api.media_calls == [] and original.read_bytes() == b'preserved-original'
    with engine.store.db() as db:
        changes = dict(db.execute('SELECT path,status FROM changes WHERE job=?', (key,)))
    assert changes['disk:/bad.bin'] == 'unknown' and changes['disk:/good.bin'] == 'unchanged'
    if source_mode == 'portable':
        source = Path(first['root'])/result['settings']['history_backup_local']
        query, args = 'SELECT meta FROM inventory WHERE path=?', ('disk:/bad.bin',)
    else:
        query, args = 'SELECT meta FROM entries WHERE job=? AND path=?', (first_id, 'disk:/bad.bin')
    with closing(sqlite3.connect(source)) as db:
        assert db.execute(query, args).fetchone()[0] == bad_meta


@pytest.mark.parametrize('present', [True, False])
def test_existing_malformed_baseline_stays_unknown_without_changing_original(tmp_path, present):
    store = Store(tmp_path/'state/jobs.sqlite3')
    settings = Engine(store, FakeAPI()).validate_settings({'destination':str(tmp_path/'files'), 'whole_disk':True})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES(?,?,?,?,?,?,1)',
                   ('job', json.dumps(settings), str(tmp_path/'files'), 'service', 'ready', '2026-01-01'))
        db.execute("INSERT INTO baseline VALUES('job','disk:/old.bin','file',CAST(X'80' AS TEXT))")
        if present:
            db.execute("INSERT INTO entries(job,path,kind,meta) VALUES('job','disk:/old.bin','file','{\"size\":1}')")
    compare(store, 'job')
    with store.db() as db:
        assert db.execute("SELECT status FROM changes WHERE job='job'").fetchone()[0] == 'unknown'
        assert db.execute("SELECT CAST(meta AS BLOB) FROM baseline WHERE job='job'").fetchone()[0] == b'\x80'


@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
def test_saved_settings_follow_database_encoding(tmp_path, encoding):
    filename = tmp_path/'history.sqlite3'
    with closing(sqlite3.connect(filename)) as db, db:
        db.execute("PRAGMA encoding='"+encoding+"'")
        db.execute('CREATE TABLE encoding_fixture(id INTEGER)')
    store = Store(filename)
    settings = Engine(store, FakeAPI()).validate_settings({'destination':str(tmp_path/'файлы'), 'whole_disk':True})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('job', json.dumps(settings,ensure_ascii=False), str(tmp_path/'файлы'), 'service', 'ready', '2026-01-01'))
        db.execute('INSERT INTO profiles VALUES(?,?)', ('профиль',json.dumps(settings,ensure_ascii=False)))
    reopened = Store(filename)
    assert reopened.job('job')['settings']['destination'] == settings['destination']
    assert list(reopened.profile_rows())[0]['settings']['destination'] == settings['destination']


def test_unknown_baseline_never_authorizes_a_corrupted_content_copy(tmp_path):
    api = FakeAPI()
    expected = b'expected-original'
    api.file('disk:/photo.bin', expected, preview=False)
    engine = Engine(Store(tmp_path/'first/jobs.sqlite3'),api)
    first_id = engine.create({'destination':str(tmp_path/'files'), 'whole_disk':True, 'mode':'fallback',
                              'reports':['json'], 'create_zip':False, 'auto_start':True})
    first = wait_job(engine, first_id)
    with engine.store.db() as db:
        old_local = db.execute('SELECT local FROM artifacts WHERE job=?',(first_id,)).fetchone()[0]
    original = Path(first['root'])/old_local
    original.write_bytes(b'x'*len(expected))
    manifest = Path(first['root'])/first['settings']['manifest_local']
    with closing(sqlite3.connect(manifest)) as db, db:
        db.execute("UPDATE inventory SET meta='[]' WHERE kind='file'")
    fresh = Engine(Store(tmp_path/'fresh/jobs.sqlite3'),api)
    api.media_calls.clear()
    key = fresh.create({'destination':first['root'], 'operation':'missing', 'mode':'fallback',
                        'reports':['json'], 'create_zip':False, 'auto_start':True})
    result = wait_job(fresh,key)
    assert result['phase'] == 'completed'
    assert api.media_calls == [('disk:/photo.bin','original')]
    with fresh.store.db() as db:
        local = db.execute('SELECT local FROM artifacts WHERE job=?',(key,)).fetchone()[0]
    assert (Path(first['root'])/local).read_bytes() == expected
    assert original.read_bytes() == b'x'*len(expected)


def test_backup_wait_for_exclusive_source_is_bounded(tmp_path, monkeypatch):
    from disk_downloader import backups as module
    monkeypatch.setattr('disk_downloader.store.journal_mode',lambda:'DELETE')
    monkeypatch.setattr(module,'BACKUP_TIMEOUT',.3,raising=False)
    monkeypatch.setattr(module,'BACKUP_BUSY_TIMEOUT',.15,raising=False)
    store = Store(tmp_path/'state/jobs.sqlite3')
    backups = module.Backups(store)
    done = threading.Event()
    errors = []
    def create():
        try:backups.create()
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with closing(sqlite3.connect(store.filename)) as holder:
        holder.execute('BEGIN EXCLUSIVE')
        worker = threading.Thread(target=create,daemon=True)
        worker.start()
        try:
            bounded = done.wait(1.5)
        finally:
            holder.rollback()
            worker.join(3)
    assert not worker.is_alive()
    assert bounded, 'Backup kept waiting beyond its operation budget'
    assert len(errors) == 1 and isinstance(errors[0],TimeoutError),errors
    assert not list(backups.folder.glob('.backup-*'))
    with store.db() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_backup_wait_for_other_backup_lock_is_bounded(tmp_path, monkeypatch):
    from disk_downloader import backups as module
    monkeypatch.setattr(module,'BACKUP_TIMEOUT',.3,raising=False)
    monkeypatch.setattr(module,'BACKUP_BUSY_TIMEOUT',.15,raising=False)
    backups = module.Backups(Store(tmp_path/'state/jobs.sqlite3'))
    done = threading.Event()
    errors = []
    def create():
        try:backups.create()
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with module.LOCK:
        worker = threading.Thread(target=create,daemon=True)
        worker.start()
        bounded = done.wait(1.5)
    worker.join(3)
    assert not worker.is_alive()
    assert bounded, 'Backup lock has no bounded admission'
    assert len(errors) == 1 and isinstance(errors[0],TimeoutError),errors


@pytest.mark.parametrize('query',['plan','folders'])
@pytest.mark.parametrize('mode',['WAL','DELETE'])
def test_readonly_queries_never_own_writer_gate(tmp_path, monkeypatch, query, mode):
    from disk_downloader.insights import download_plan
    from disk_downloader.inventory import folders
    if mode == 'DELETE':monkeypatch.setattr('disk_downloader.store.journal_mode',lambda:'DELETE')
    store = Store(tmp_path/'state/jobs.sqlite3')
    if mode == 'WAL' and not store.wal:pytest.skip('Patched WAL required')
    settings = Engine(store,FakeAPI()).validate_settings({'destination':str(tmp_path/'files'),'whole_disk':True})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('job',json.dumps(settings),str(tmp_path/'files'),'service','ready','2026-01-01'))
        db.executemany('INSERT INTO entries(job,path,kind,meta) VALUES(?,?,?,?)',
                       (('job',f'disk:/{i:05}.bin','file','{"size":1,"file_type":"files"}') for i in range(1500)))
    entered,release,done = threading.Event(),threading.Event(),threading.Event()
    errors = []
    original = store._connect
    def connect():
        db = original()
        if threading.current_thread().name == 'read-only-report':
            def progress():
                if not entered.is_set():
                    entered.set()
                    assert release.wait(5)
                return 0
            db.set_progress_handler(progress,1000)
        return db
    monkeypatch.setattr(store,'_connect',connect)
    def reader():
        try:(download_plan if query == 'plan' else folders)(store,'job')
        except Exception as exc:errors.append(exc)
    def writer():
        try:
            with store.db() as db:db.execute("INSERT INTO preferences VALUES('writer','committed')")
        except Exception as exc:errors.append(exc)
        finally:done.set()
    first = threading.Thread(target=reader,name='read-only-report',daemon=True)
    second = threading.Thread(target=writer,daemon=True)
    first.start()
    try:
        assert entered.wait(3)
        owner = store.write_gate.owner
        second.start()
        concurrent_commit = done.wait(.3)
    finally:
        release.set()
        first.join(5)
        if second.ident:second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert owner is None, 'Read-only SQL acquired application writer admission'
    assert mode == 'DELETE' or concurrent_commit
    assert done.is_set() and not errors,errors


@pytest.mark.parametrize('parser',['settings','history'])
def test_recursive_json_parser_failure_is_isolated(tmp_path, monkeypatch, parser):
    from disk_downloader.import_state import history_metadata, UNREADABLE_META
    original = json.loads
    def load(value, *args, **kwargs):
        if value == 'recursion-fixture':raise RecursionError('synthetic parser depth limit')
        return original(value,*args,**kwargs)
    monkeypatch.setattr(json,'loads',load)
    if parser == 'history':
        text,damaged = history_metadata('recursion-fixture')
        assert damaged and json.loads(text) == {UNREADABLE_META:True}
    else:
        store = Store(tmp_path/'state/jobs.sqlite3')
        with pytest.raises(ValueError):store.settings_value('recursion-fixture')


def test_restore_wait_for_writer_admission_is_bounded(tmp_path, monkeypatch):
    from disk_downloader import backups as module
    monkeypatch.setattr(module,'BACKUP_TIMEOUT',.3)
    monkeypatch.setattr(module,'BACKUP_BUSY_TIMEOUT',.15)
    store = Store(tmp_path/'state/jobs.sqlite3')
    backups = module.Backups(store)
    name = backups.create()
    done = threading.Event()
    errors = []
    def restore():
        try:backups.restore(name)
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with store.db() as holder:
        holder.execute("INSERT INTO preferences VALUES('uncommitted','preserved')")
        worker = threading.Thread(target=restore,daemon=True)
        worker.start()
        bounded = done.wait(1.5)
    worker.join(3)
    assert not worker.is_alive()
    assert bounded,'Restore kept waiting for the in-process writer without its operation budget'
    assert len(errors) == 1 and isinstance(errors[0],TimeoutError),errors
    assert store.write_gate.owner is None and not store.write_gate.waiting
    with store.db() as db:
        assert db.execute("SELECT value FROM preferences WHERE key='uncommitted'").fetchone()[0] == 'preserved'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_restore_commit_wait_is_bounded_and_rolls_back(tmp_path, monkeypatch):
    from disk_downloader import backups as module
    monkeypatch.setattr('disk_downloader.store.journal_mode',lambda:'DELETE')
    monkeypatch.setattr(module,'BACKUP_TIMEOUT',.3)
    monkeypatch.setattr(module,'BACKUP_BUSY_TIMEOUT',.15)
    store = Store(tmp_path/'state/jobs.sqlite3')
    backups = module.Backups(store)
    name = backups.create()
    with store.db() as db:db.execute("INSERT INTO preferences VALUES('live','retained')")
    done = threading.Event()
    errors = []
    def restore():
        try:backups.restore(name)
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with closing(sqlite3.connect(store.filename)) as holder:
        holder.execute('BEGIN')
        holder.execute('SELECT * FROM preferences').fetchall()
        worker = threading.Thread(target=restore,daemon=True)
        worker.start()
        try:bounded = done.wait(1.5)
        finally:
            holder.rollback()
            worker.join(3)
    assert not worker.is_alive()
    assert bounded,'Restore commit ignored its operation deadline'
    assert len(errors) == 1 and isinstance(errors[0],TimeoutError),errors
    with store.db() as db:
        assert db.execute("SELECT value FROM preferences WHERE key='live'").fetchone()[0] == 'retained'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_cancelled_connection_backup_preserves_target_and_timeouts(tmp_path):
    from disk_downloader.backups import backup_connection
    with closing(sqlite3.connect(tmp_path/'source.sqlite3')) as source, closing(sqlite3.connect(tmp_path/'target.sqlite3')) as target:
        source.execute('CREATE TABLE payload(data BLOB)')
        source.executemany('INSERT INTO payload VALUES(?)',[(b'x'*1024*1024,)]*4)
        source.commit()
        target.execute('CREATE TABLE retained(data TEXT)')
        target.execute("INSERT INTO retained VALUES('previous database')")
        target.commit()
        source.execute('PRAGMA busy_timeout=1234')
        target.execute('PRAGMA busy_timeout=2345')
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            if calls == 5:raise InterruptedError('synthetic stop during page copy')
        with pytest.raises(InterruptedError):backup_connection(source,target,checkpoint=cancel)
        assert calls == 5
        assert target.execute('SELECT data FROM retained').fetchone()[0] == 'previous database'
        assert source.execute('SELECT COUNT(*) FROM payload').fetchone()[0] == 4
        assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert source.execute('PRAGMA busy_timeout').fetchone()[0] == 1234
        assert target.execute('PRAGMA busy_timeout').fetchone()[0] == 2345


def test_cancelled_snapshot_keeps_completed_backup_and_removes_only_its_temp(tmp_path):
    from disk_downloader.backups import Backups
    store = Store(tmp_path/'state/jobs.sqlite3')
    backups = Backups(store)
    name = backups.create()
    previous = backups.path(name).read_bytes()
    foreign = backups.folder/'.backup-user-file'
    foreign.write_bytes(b'preserved unrelated file')
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 4:raise InterruptedError('synthetic shutdown')
    with pytest.raises(InterruptedError):backups.create(checkpoint=cancel)
    assert backups.path(name).read_bytes() == previous
    assert foreign.read_bytes() == b'preserved unrelated file'
    assert list(backups.folder.glob('.backup-*')) == [foreign]


def test_cancelled_restore_rolls_back_all_changes_and_keeps_safety_copy(tmp_path, monkeypatch):
    from disk_downloader.backups import Backups
    store = Store(tmp_path/'state/jobs.sqlite3')
    backups = Backups(store)
    name = backups.create()
    saved = backups.path(name).read_bytes()
    for number in range(10):
        (backups.folder/('backup-2026-01-01T00-00-00Z-'+format(number,'08x')+'.sqlite3')).write_bytes(saved)
    previous = {row['name']:backups.path(row['name']).read_bytes() for row in backups.list()}
    with store.db() as db:db.execute("INSERT INTO profiles VALUES('live','{}')")
    original = store._connect
    destructive = threading.Event()
    def connection():
        db = original()
        def trace(sql):
            if sql.startswith('DELETE FROM "jobs"'):destructive.set()
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(store,'_connect',connection)
    def cancel():
        if destructive.is_set():raise InterruptedError('synthetic shutdown during restore')
    with pytest.raises(InterruptedError):backups.restore(name,checkpoint=cancel)
    assert destructive.is_set() and len(backups.list()) == len(previous)+1
    assert all(backups.path(name).read_bytes() == data for name,data in previous.items())
    with store.db() as db:
        assert db.execute('SELECT name FROM profiles').fetchone()[0] == 'live'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert store.write_gate.owner is None and not store.write_gate.waiting


@pytest.mark.parametrize('table',['jobs','profiles'])
def test_bad_backup_settings_explains_error_and_preserves_current_state(tmp_path, table):
    from disk_downloader.backups import Backups
    store = Store(tmp_path/'state/jobs.sqlite3')
    settings = Engine(store,FakeAPI()).validate_settings({'destination':str(tmp_path/'files'),'whole_disk':True})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('live',json.dumps(settings),str(tmp_path/'files'),'service','ready','2026-01-01'))
        db.execute("INSERT INTO profiles VALUES('live','{}')")
    backups = Backups(store)
    name = backups.create()
    with closing(sqlite3.connect(backups.path(name))) as source,source:
        source.execute('UPDATE '+table+" SET settings=CAST(X'80' AS TEXT)")
    original = backups.path(name).read_bytes()
    with pytest.raises(ValueError,match='Текущая история не изменена'):backups.restore(name)
    assert backups.path(name).read_bytes() == original
    assert store.job('live')['settings']['destination'] == settings['destination']


@pytest.mark.parametrize('failed',[False,True])
def test_restore_does_not_hold_engine_lock_and_blocks_mutations(tmp_path, monkeypatch, failed):
    from test_api_security import web_session
    app,client,base,headers = web_session(tmp_path)
    engine = app.extensions['engine']
    recovery = app.extensions['recovery']
    if recovery.thread:recovery.thread.join(3)
    entered,release = threading.Event(),threading.Event()
    responses = []
    def restore(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        if failed:raise TimeoutError('synthetic bounded timeout')
        return 'safety.sqlite3'
    monkeypatch.setattr(app.extensions['backups'],'restore',restore)
    def request():
        with app.test_client() as worker:
            with worker.session_transaction(base_url=base) as session:session['csrf'] = headers['X-CSRF-Token']
            responses.append(worker.post('/api/backups/restore',base_url=base,headers=headers,json={'name':'unused','confirm':True}))
    worker = threading.Thread(target=request,daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        assert engine.maintenance_busy
        assert engine.history_restore_busy
        acquired = engine.lock.acquire(timeout=.3)
        if acquired:engine.lock.release()
        assert acquired,'A slow backup restore held the application control lock'
        assert client.get('/api/status',base_url=base,headers=headers).status_code == 200
        for url,data in (('/api/queue',{'action':'resume'}),
                         ('/api/profiles',{'name':'live','settings':{'whole_disk':True,'destination':str(tmp_path/'new')}}),
                         ('/api/profiles',{'name':'live','delete':True}),
                         ('/api/jobs',{'whole_disk':True,'destination':str(tmp_path/'new')})):
            response = client.post(url,base_url=base,headers=headers,json=data)
            assert response.status_code == 400,(url,response.json)
            assert 'восстанавливает историю' in response.json['error'],(url,response.json)
        assert client.post('/api/connect',base_url=base,headers=headers,json={'token':'valid'}).status_code == 400
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not engine.maintenance_busy
    assert not engine.history_restore_busy
    assert len(responses) == 1 and responses[0].status_code == (400 if failed else 200)


@pytest.mark.parametrize('operation',['portable','migration'])
def test_history_and_migration_snapshots_have_bounded_backup_waits(tmp_path, monkeypatch, operation):
    from disk_downloader import backups as module
    from disk_downloader.import_state import backup_import
    from disk_downloader.migration import backup_snapshot
    monkeypatch.setattr(module,'BACKUP_TIMEOUT',.3)
    monkeypatch.setattr(module,'BACKUP_BUSY_TIMEOUT',.15)
    store = Store(tmp_path/'state/jobs.sqlite3')
    root = tmp_path/'files'
    root.mkdir()
    settings = Engine(store,FakeAPI()).validate_settings({'destination':str(root),'whole_disk':True})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('job',json.dumps(settings),str(root),'service','ready','2026-01-01'))
    legacy = tmp_path/'legacy.sqlite3'
    with closing(sqlite3.connect(legacy)) as db,db:db.execute('CREATE TABLE original(data TEXT)')
    previous = legacy.read_bytes()
    done = threading.Event()
    errors = []
    def copy():
        try:
            if operation == 'portable':backup_import(store,'job',{'file':legacy})
            else:
                with closing(module.readonly(legacy)) as source,backup_snapshot(source,tmp_path):pass
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with closing(sqlite3.connect(legacy)) as holder:
        holder.execute('BEGIN EXCLUSIVE')
        worker = threading.Thread(target=copy,daemon=True)
        worker.start()
        try:bounded = done.wait(1.5)
        finally:
            holder.rollback()
            worker.join(3)
    assert not worker.is_alive() and bounded,'This SQLite backup call bypassed its operation budget'
    assert len(errors) == 1 and isinstance(errors[0],TimeoutError),errors
    assert legacy.read_bytes() == previous
    assert not list(root.glob('.disk-history-before-recovery*.sqlite3'))
    assert not list(tmp_path.glob('.migration-*.sqlite3'))
    with store.db() as db:assert db.execute('SELECT COUNT(*) FROM temporary_files').fetchone()[0] == 0


def test_backup_timeout_http_message_names_history_and_next_action(tmp_path, monkeypatch):
    from disk_downloader.backups import BackupBudget
    from test_api_security import web_session
    app,client,base,headers = web_session(tmp_path)
    def timeout(*args,**kwargs):BackupBudget(timeout=-1).check()
    monkeypatch.setattr(app.extensions['backups'],'create',timeout)
    response = client.post('/api/backups',base_url=base,headers=headers,json={})
    assert response.status_code == 400
    assert 'историю' in response.json['error']
    assert 'Повторите' in response.json['error']
    assert 'Код: 0' not in response.json['error']


def test_profiles_and_queue_remain_editable_during_offline_file_verification(tmp_path, monkeypatch):
    import disk_downloader.maintenance as module
    from test_api_security import web_session
    app,client,base,headers = web_session(tmp_path)
    engine = app.extensions['engine']
    app.extensions['disk_api'].file('disk:/kept.bin',b'preserved file',preview=False)
    key = engine.create({'destination':str(tmp_path/'files'),'whole_disk':True,'mode':'fallback',
                         'auto_start':True,'auto_pause':False,'reports':['json'],'create_zip':False})
    assert wait_job(engine,key)['phase'] == 'completed'
    entered,release = threading.Event(),threading.Event()
    signature = module.file_signature
    def slow_signature(*args,**kwargs):
        entered.set()
        assert release.wait(10)
        return signature(*args,**kwargs)
    monkeypatch.setattr(module,'file_signature',slow_signature)
    response = client.post('/api/verification',base_url=base,headers=headers,json={'job':key})
    assert response.status_code == 200,response.json
    maintenance = app.extensions['maintenance']
    try:
        assert entered.wait(3) and engine.maintenance_busy
        response = client.post('/api/profiles',base_url=base,headers=headers,
                               json={'name':'during verification','settings':{'whole_disk':True,'destination':str(tmp_path/'later')}})
        assert response.status_code == 200,response.json
        response = client.get('/api/profiles',base_url=base,headers=headers)
        assert any(row['name'] == 'during verification' for row in response.json['items'])
        response = client.post('/api/profiles',base_url=base,headers=headers,json={'name':'during verification','delete':True})
        assert response.status_code == 200,response.json
        for data in ({'action':'add','job':key},{'action':'pause'},{'action':'resume'},
                     {'action':'reorder','jobs':[key]},{'action':'remove','job':key}):
            response = client.post('/api/queue',base_url=base,headers=headers,json=data)
            assert response.status_code == 200,(data,response.json)
        app.extensions['management'].tick()
        assert engine.maintenance_busy and not engine.threads[key].is_alive()
    finally:
        release.set()
        maintenance.thread.join(10)
    assert not maintenance.thread.is_alive() and maintenance.snapshot()['complete']


def deep_state(tmp_path):
    state = tmp_path/'Папка #100% с пробелом'
    while len(str(state))+65 < 215:state = state/('s'*64)
    state = state/('s'*(215-len(str(state))-1))
    assert len(str(state)) == 215
    state.mkdir(parents=True)
    return state


@pytest.mark.skipif(os.name != 'nt',reason='Native Windows MAX_PATH filesystem behavior')
def test_deep_state_backup_create_readonly_restore_and_before_upgrade(tmp_path):
    from disk_downloader.backups import Backups, before_upgrade, readonly
    from disk_downloader.paths import native
    state = deep_state(tmp_path)
    store = Store(state/'jobs.sqlite3')
    with store.db() as db:db.execute("INSERT INTO profiles VALUES('previous','{}')")
    backups = Backups(store)
    name = backups.create()
    file = backups.path(name)
    assert len(str(file)) > 260
    original = file.read_bytes()
    with closing(readonly(file)) as db:assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    import io
    imported = backups.import_file(io.BytesIO(original))
    assert backups.path(imported).read_bytes() == original
    with store.db() as db:
        db.execute("DELETE FROM profiles")
        db.execute("INSERT INTO profiles VALUES('current','{}')")
    safety = backups.restore(name)
    assert backups.path(name).read_bytes() == original
    with closing(readonly(backups.path(safety))) as db:
        assert db.execute('SELECT name FROM profiles').fetchone()[0] == 'current'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with store.db() as db:
        assert db.execute('SELECT name FROM profiles').fetchone()[0] == 'previous'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    before_upgrade(Path(store.filename))
    upgrade = list(Path(native(state/'backups')).glob('before-*.sqlite3'))
    assert upgrade
    with closing(readonly(upgrade[0])) as db:
        assert db.execute('SELECT name FROM profiles').fetchone()[0] == 'previous'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not list(backups.folder.glob('.backup-*'))


@pytest.mark.skipif(os.name != 'nt',reason='Native Windows MAX_PATH filesystem behavior')
def test_cancelled_deep_state_backup_preserves_completed_copy(tmp_path):
    from disk_downloader.backups import Backups
    store = Store(deep_state(tmp_path)/'jobs.sqlite3')
    backups = Backups(store)
    name = backups.create()
    previous = backups.path(name).read_bytes()
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 5:raise InterruptedError('synthetic interrupted deep backup')
    with pytest.raises(InterruptedError):backups.create(checkpoint=cancel)
    assert backups.path(name).read_bytes() == previous
    assert not list(backups.folder.glob('.backup-*'))
    with store.db() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('name',['данные #100% с пробелом.sqlite3','literal?query.sqlite3'])
def test_backup_readonly_native_uri_encoding_and_noncreation(tmp_path, name):
    from disk_downloader.backups import readonly, recovery_source
    from disk_downloader.paths import native
    if os.name == 'nt' and '?' in name:pytest.skip('Question mark is not a valid Windows filename component')
    file = tmp_path/name
    with closing(sqlite3.connect(native(file))) as db,db:
        db.execute('CREATE TABLE marker(value TEXT)')
        db.execute("INSERT INTO marker VALUES('preserved')")
    before = file.read_bytes()
    with closing(readonly(native(file))) as db:
        assert db.execute('SELECT value FROM marker').fetchone()[0] == 'preserved'
        with pytest.raises(sqlite3.OperationalError):db.execute("INSERT INTO marker VALUES('forbidden')")
    assert file.read_bytes() == before
    missing = tmp_path/('missing '+name)
    for opener in (readonly,recovery_source):
        with pytest.raises(sqlite3.OperationalError):opener(native(missing))
        assert not Path(native(missing)).exists()


def test_imported_wal_backup_is_standalone_without_private_sidecars(tmp_path):
    import io
    from disk_downloader.backups import Backups, readonly
    store = Store(tmp_path/'state/jobs.sqlite3')
    if not store.wal:pytest.skip('WAL source required for standalone import regression')
    with store.db() as db:
        db.execute("INSERT INTO profiles VALUES('preserved','{}')")
        mode = db.execute('PRAGMA journal_mode').fetchone()[0]
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
    original = Path(store.filename).read_bytes()
    assert original[18:20] == b'\x02\x02'
    stream = io.BytesIO(original)
    backups = Backups(store)
    name = backups.import_file(stream)
    assert not list(backups.folder.glob('.backup-*')),'Private import left detached SQLite WAL/SHM metadata'
    with closing(readonly(backups.path(name))) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT name FROM profiles').fetchone()[0] == 'preserved'
    assert stream.getvalue() == original and Path(store.filename).read_bytes() == original
    with store.db() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == mode
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2


@pytest.mark.parametrize('operation',['create','import'])
@pytest.mark.parametrize('when',['before','after'])
def test_cancelled_private_backup_normalization_preserves_inputs_and_previous_copy(tmp_path, monkeypatch, operation, when):
    import io
    import disk_downloader.backups as module
    store = Store(tmp_path/'state/jobs.sqlite3')
    if not store.wal:pytest.skip('WAL source required for standalone cancellation regression')
    with store.db() as db:db.execute("INSERT INTO profiles VALUES('preserved','{}')")
    backups = module.Backups(store)
    name = backups.create()
    previous = backups.path(name).read_bytes()
    original = Path(store.filename).read_bytes()
    stream = io.BytesIO(original)
    normalize = module._standalone
    calls = []
    def interrupted(db,budget):
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        calls.append(True)
        if when == 'after':normalize(db,budget)
        raise InterruptedError('synthetic private normalization interruption')
    monkeypatch.setattr(module,'_standalone',interrupted)
    with pytest.raises(InterruptedError):
        if operation == 'create':backups.create()
        else:backups.import_file(stream)
    assert len(calls) == 1
    assert backups.path(name).read_bytes() == previous
    assert [item['name'] for item in backups.list()] == [name]
    assert not list(backups.folder.glob('.backup-*'))
    assert stream.getvalue() == original and Path(store.filename).read_bytes() == original
    with store.db() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT name FROM profiles').fetchone()[0] == 'preserved'
