"""Unreadable old metadata must not prevent verification of intact copies."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from conftest import FakeAPI, run_job, wait_job
from disk_downloader.engine import Engine
from disk_downloader.import_state import backup_import, find_import, inherit
from disk_downloader.store import Store


@pytest.mark.parametrize('source_mode', ['wal', 'delete', 'portable'])
def test_unreadable_old_metadata_recovers_without_changing_saved_copy(tmp_path, monkeypatch, source_mode):
    if source_mode == 'delete':
        monkeypatch.setattr('disk_downloader.store.journal_mode', lambda:'DELETE')
    api = FakeAPI()
    api.file('disk:/kept.jpg', b'preserved-original')
    engine = Engine(Store(tmp_path/'state/jobs.sqlite3'), api)
    first = run_job((engine,api,tmp_path), mode='fallback')
    with engine.store.db() as db:
        relative = db.execute('SELECT local FROM artifacts WHERE job=?', (first['id'],)).fetchone()[0]
    file = Path(first['root'])/relative
    before = file.read_bytes()
    if source_mode == 'portable':
        filename = Path(first['root'])/engine.store.job(first['id'])['settings']['manifest_local']
        with closing(sqlite3.connect(filename)) as db, db:
            db.execute("UPDATE inventory SET meta=CAST(x'ff' AS TEXT) WHERE path='disk:/kept.jpg'")
        engine = Engine(Store(tmp_path/'fresh/jobs.sqlite3'), api)
    else:
        with engine.store.db() as db:
            db.execute("UPDATE entries SET meta=CAST(x'ff' AS TEXT) WHERE job=? AND path='disk:/kept.jpg'", (first['id'],))
        filename = Path(engine.store.filename)
    # This is a Python SQLite decoding failure, without an engine error code.
    table = 'inventory' if source_mode == 'portable' else 'entries'
    where = " WHERE path='disk:/kept.jpg'" + ('' if source_mode == 'portable' else ' AND job=?')
    args = () if source_mode == 'portable' else (first['id'],)
    with closing(sqlite3.connect(filename)) as db:
        with pytest.raises(sqlite3.OperationalError, match='Could not decode to UTF-8') as failure:
            list(db.execute('SELECT path,kind,meta FROM '+table+where, args))
    assert not hasattr(failure.value, 'sqlite_errorcode')
    api.media_calls.clear()
    job_id = engine.create({'operation':'missing','destination':first['root'],'create_zip':False})
    ready = wait_job(engine, job_id)
    assert ready['phase'] == 'ready', ready.get('error')
    assert ready['originals'] == 1 and ready['processed'] == 1
    assert ready['errors'] == 0 and ready['scan_errors'] == 0
    assert ready['settings']['history_warning']
    if source_mode == 'portable':
        filename = Path(first['root'])/ready['settings']['history_backup_local']
        assert filename.is_file()
    with engine.store.db() as db:
        assert db.execute('SELECT status FROM changes WHERE job=? AND path=?', (job_id,'disk:/kept.jpg')).fetchone()[0] == 'unknown'
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    engine.action(job_id, 'start')
    assert wait_job(engine, job_id)['phase'] == 'completed'
    assert api.media_calls == [] and file.read_bytes() == before
    with closing(sqlite3.connect(filename)) as db:
        assert db.execute('SELECT CAST(meta AS BLOB) FROM '+table+where, args).fetchone()[0] == b'\xff'


def test_rollback_snapshot_can_spill_binary_values_without_holding_read_lock(tmp_path, monkeypatch):
    monkeypatch.setattr('disk_downloader.store.journal_mode', lambda:'DELETE')
    store = Store(tmp_path/'state.sqlite3')
    binary = bytes(range(256))*2048
    text = json.dumps({'__sqlite_blob__':'not a blob'})
    with store.db() as db:
        db.execute('CREATE TABLE fixture(id INTEGER, payload BLOB, label TEXT)')
        db.executemany('INSERT INTO fixture VALUES(?,?,?)',[(i,binary,text) for i in range(4)])
    iterator = store.rows('SELECT * FROM fixture ORDER BY id', batch=2)
    first = next(iterator)
    with store.db() as db:
        db.execute("UPDATE fixture SET label='after'")
    rows = [first,*iterator]
    assert len(rows) == 4
    assert all(row['payload'] == binary and row['label'] == text for row in rows)


@pytest.mark.parametrize('encoding', ['UTF-8','UTF-16le','UTF-16be'])
def test_valid_inventory_encoding_is_not_marked_unreadable(tmp_path, encoding):
    # SQLite CAST AS BLOB uses the database encoding, not always UTF-8.
    store = Store(tmp_path/'state.sqlite3')
    settings = {'paths':['disk:/']}
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('target',json.dumps(settings),str(tmp_path),'service','new','2026-01-01'))
    file = tmp_path/'inventory.sqlite3'
    text = json.dumps({'name':'Пример','type':'file','size':3},ensure_ascii=False)
    with closing(sqlite3.connect(file)) as db, db:
        db.execute("PRAGMA encoding='"+encoding+"'")
        db.execute('CREATE TABLE inventory(path TEXT,kind TEXT,meta TEXT)')
        db.execute('INSERT INTO inventory VALUES(?,?,?)',('disk:/example.jpg','file',text))
    result = inherit(store,'target',{'file':file})
    assert result['unreadable_metadata'] == 0
    with store.db() as db:
        assert db.execute("SELECT meta FROM baseline WHERE job='target'").fetchone()[0] == text


def test_cancelled_backup_keeps_source_and_cleans_only_its_temporary_file(system):
    from disk_downloader.api import Suspended
    engine,api,_ = system
    api.file('disk:/kept.jpg')
    first = run_job(system)
    manifest = Path(first['root'])/engine.store.job(first['id'])['settings']['manifest_local']
    previous = {'file':manifest}
    before = manifest.read_bytes()
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls > 1:raise Suspended('Stopped')
    with pytest.raises(Suspended):backup_import(engine.store,first['id'],previous,cancel)
    assert manifest.read_bytes() == before
    assert not list(Path(first['root']).glob('.disk-history-before-recovery*.sqlite3'))
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM temporary_files').fetchone()[0] == 0


def test_unreadable_baseline_is_not_reported_as_a_confirmed_removal(system):
    engine, api, _ = system
    api.file('disk:/kept.jpg', b'preserved-original')
    first = run_job(system, mode='fallback')
    with engine.store.db() as db:
        local = db.execute('SELECT local FROM artifacts WHERE job=?', (first['id'],)).fetchone()[0]
        db.execute("UPDATE entries SET meta=CAST(x'ff' AS TEXT) WHERE job=? AND kind='file'", (first['id'],))
    del api.tree['disk:/kept.jpg']
    job_id = engine.create({'operation':'missing','destination':first['root']})
    assert wait_job(engine, job_id)['phase'] == 'ready'
    with engine.store.db() as db:
        assert db.execute('SELECT status FROM changes WHERE job=? AND path=?', (job_id,'disk:/kept.jpg')).fetchone()[0] == 'unknown'
    assert Path(first['root'], local).read_bytes() == b'preserved-original'


def test_recovery_backup_never_replaces_the_live_import_candidate(system):
    import os
    engine, api, tmp = system
    api.file('disk:/kept.jpg')
    first = run_job(system)
    original = Path(first['root'])/engine.store.job(first['id'])['settings']['manifest_local']
    relative = backup_import(engine.store,first['id'],{'file':original})
    backup = Path(first['root'])/relative
    newer = original.stat().st_mtime+60
    os.utime(backup,(newer,newer))
    fresh = Store(tmp/'fresh-lookup/jobs.sqlite3')
    assert find_import(fresh, first['root'])['file'].name == original.name
