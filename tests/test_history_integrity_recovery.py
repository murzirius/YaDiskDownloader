"""Actual index corruption and verified recovery; all data are synthetic."""
from contextlib import closing
import hashlib,json,sqlite3,os
from pathlib import Path
import pytest
from disk_downloader import backups,startup
from disk_downloader.history_integrity import HistoryIntegrityError,inspect,require
from disk_downloader.store import SCHEMA


def history(path,rows=64):
    with closing(sqlite3.connect(path)) as db,db:
        db.executescript(SCHEMA)
        db.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY,value TEXT)')
        db.execute('CREATE INDEX fixture_index ON fixture(value)')
        page=db.execute("SELECT rootpage FROM sqlite_master WHERE name='fixture_index'").fetchone()[0]
        size=db.execute('PRAGMA page_size').fetchone()[0]
    empty=path.read_bytes()[(page-1)*size:page*size]
    with closing(sqlite3.connect(path)) as db,db:
        db.executemany('INSERT INTO fixture(value) VALUES(?)',[(f'value-{i:04}',) for i in range(rows)])
        db.execute("INSERT INTO profiles VALUES('kept','{}')")
    return page,size,empty


def corrupt(path):
    page,size,empty=history(path)
    with path.open('r+b') as stream:stream.seek((page-1)*size);stream.write(empty)
    with closing(sqlite3.connect(path)) as db:
        info=inspect(db);assert not info['ok'] and info['index_candidate']
        assert db.execute('SELECT COUNT(*) FROM fixture NOT INDEXED').fetchone()[0]==64
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_integrity_classification_has_no_raw_names_or_values(tmp_path):
    path=tmp_path/'history.sqlite3';corrupt(path)
    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(HistoryIntegrityError) as value:require(db)
    report=startup.failure(value.value,state=tmp_path/'report')
    data=json.loads(report['report'].read_text(encoding='utf-8'))
    assert data['cause']=='history_integrity' and data['integrity']['index_candidate']
    assert 'fixture' not in json.dumps(data) and 'value-' not in json.dumps(data)


@pytest.mark.parametrize('decision',[None,False])
def test_bad_indexes_never_repair_without_confirmation(tmp_path,decision):
    path=tmp_path/'jobs.sqlite3';before=corrupt(path);seen=[]
    def decline(info):
        seen.append(info)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before
        return decision
    with pytest.raises(HistoryIntegrityError):backups.before_upgrade(path,recovery=decline if decision is not None else None)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert not list((tmp_path/'backups').glob('before-*.sqlite3'))
    assert not list((tmp_path/'backups').glob('.backup-*'))
    if decision is False:assert seen==[{'method':'indexes'}]


@pytest.mark.parametrize('wal',[False,True])
def test_verified_index_recovery_keeps_current_rows_and_original_snapshot(tmp_path,wal):
    path=tmp_path/'jobs.sqlite3';before=corrupt(path)
    if wal:
        with closing(sqlite3.connect(path)) as db:db.execute('PRAGMA journal_mode=WAL')
    seen=[]
    def accept(info):seen.append(info);return True
    backups.before_upgrade(path,recovery=accept)
    assert seen==[{'method':'indexes'}]
    with closing(sqlite3.connect(path)) as db:
        assert inspect(db)['ok']
        assert db.execute('SELECT COUNT(*) FROM fixture INDEXED BY fixture_index').fetchone()[0]==64
        assert db.execute('SELECT value FROM fixture ORDER BY id').fetchall()==[(f'value-{i:04}',) for i in range(64)]
        assert db.execute('SELECT name FROM profiles').fetchone()[0]=='kept'
    original=list((tmp_path/'backups/recovery-originals').glob('original-*.sqlite3'));assert len(original)==1
    with closing(sqlite3.connect(original[0])) as db:
        assert not inspect(db)['ok']
        assert db.execute('SELECT COUNT(*) FROM fixture NOT INDEXED').fetchone()[0]==64
    saved=list((tmp_path/'backups').glob('before-*.sqlite3'));assert len(saved)==1
    with closing(sqlite3.connect(saved[0])) as db:assert inspect(db)['ok']
    assert not list((tmp_path/'backups').glob('.backup-*'))


def test_other_writer_during_confirmation_cancels_recovery(tmp_path):
    path=tmp_path/'jobs.sqlite3';corrupt(path)
    def writer(info):
        with closing(sqlite3.connect(path)) as db,db:db.execute("INSERT INTO profiles VALUES('newer','{}')")
        return True
    with pytest.raises(ValueError,match='изменилась'):backups.before_upgrade(path,recovery=writer)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM profiles WHERE name='newer'").fetchone()[0]==1
        assert not inspect(db)['ok']


def test_exclusive_destination_lock_blocks_external_writer_during_apply(tmp_path,monkeypatch):
    path=tmp_path/'jobs.sqlite3';corrupt(path);original=backups.backup_connection;checked=[]
    def copying(source,target,*args,**kwargs):
        names=[row[2] for row in target.execute('PRAGMA database_list')]
        if any(name and os.path.samefile(name,path) for name in names) and len(checked)==0:
            with closing(sqlite3.connect(path,timeout=.01)) as other:
                with pytest.raises(sqlite3.OperationalError,match='locked'):other.execute("INSERT INTO profiles VALUES('concurrent','{}')")
            checked.append(True)
        return original(source,target,*args,**kwargs)
    monkeypatch.setattr(backups,'backup_connection',copying)
    backups.before_upgrade(path,recovery=lambda _:True)
    assert checked==[True]


def test_failed_apply_keeps_original_and_its_safety_snapshot(tmp_path,monkeypatch):
    path=tmp_path/'jobs.sqlite3';corrupt(path);original=backups.backup_connection
    def fail(source,target,*args,**kwargs):
        if any(row[2] and os.path.samefile(row[2],path) for row in target.execute('PRAGMA database_list')):raise OSError(5,'synthetic apply failure')
        return original(source,target,*args,**kwargs)
    monkeypatch.setattr(backups,'backup_connection',fail)
    with pytest.raises(OSError):backups.before_upgrade(path,recovery=lambda _:True)
    with closing(sqlite3.connect(path)) as db:
        assert not inspect(db)['ok'] and db.execute('SELECT COUNT(*) FROM fixture NOT INDEXED').fetchone()[0]==64
    assert list((tmp_path/'backups/recovery-originals').glob('original-*.sqlite3'))


def test_reindex_failure_offers_validated_backup_only_with_explicit_rollback_confirmation(tmp_path,monkeypatch):
    path=tmp_path/'jobs.sqlite3';corrupt(path);folder=tmp_path/'backups';folder.mkdir()
    saved=folder/'before-1.5.0.sqlite3';history(saved,rows=9)
    import disk_downloader.backups as module
    original=module.require_integrity
    def reject_reindex(db):
        info=inspect(db)
        if info['ok'] and db.execute("SELECT COUNT(*) FROM fixture").fetchone()[0]==64:
            raise HistoryIntegrityError({'ok':False,'issues':{'structure_or_other':1},'index_candidate':False})
        return original(db)
    monkeypatch.setattr(module,'require_integrity',reject_reindex)
    seen=[]
    backups.before_upgrade(path,recovery=lambda info:seen.append(info) or True)
    assert len(seen)==1 and seen[0]['method']=='backup' and seen[0]['name']=='before-1.5.0.sqlite3'
    with closing(sqlite3.connect(path)) as db:assert db.execute('SELECT COUNT(*) FROM fixture').fetchone()[0]==9
    with closing(sqlite3.connect(next((folder/'recovery-originals').glob('original-*.sqlite3')))) as db:
        assert db.execute('SELECT COUNT(*) FROM fixture NOT INDEXED').fetchone()[0]==64


@pytest.mark.parametrize('wal',[False,True])
def test_cancel_during_sqlite_apply_rolls_back_and_keeps_safety_copy(tmp_path,monkeypatch,wal):
    path=tmp_path/'jobs.sqlite3';corrupt(path)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE payload(body BLOB)')
        db.executemany('INSERT INTO payload VALUES(?)',[(b'synthetic'*131072,) for _ in range(5)])
    if wal:
        with closing(sqlite3.connect(path)) as db:db.execute('PRAGMA journal_mode=WAL')
    original=backups.backup_connection
    def copy(source,target,checkpoint=lambda:None,timeout=None):
        if any(row[2] and os.path.samefile(row[2],path) for row in target.execute('PRAGMA database_list')):
            calls=[]
            def cancel():
                calls.append(1)
                if len(calls)==3:raise InterruptedError('synthetic cancel during destination copy')
                checkpoint()
            return original(source,target,checkpoint=cancel,timeout=timeout)
        return original(source,target,checkpoint=checkpoint,timeout=timeout)
    monkeypatch.setattr(backups,'backup_connection',copy)
    with pytest.raises(InterruptedError):backups.before_upgrade(path,recovery=lambda _:True)
    with closing(sqlite3.connect(path)) as db:
        assert not inspect(db)['ok']
        assert db.execute('SELECT COUNT(*) FROM fixture NOT INDEXED').fetchone()[0]==64
        assert db.execute('SELECT COUNT(*) FROM payload').fetchone()[0]==5
    assert list((tmp_path/'backups/recovery-originals').glob('original-*.sqlite3'))
    assert not list((tmp_path/'backups').glob('before-*.sqlite3'))


@pytest.mark.parametrize('row_factory',[None,sqlite3.Row])
@pytest.mark.parametrize('text_factory',[str,bytes])
def test_integrity_accepts_canonical_ok_across_sqlite_row_factories(row_factory,text_factory):
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory=row_factory;db.text_factory=text_factory
        assert inspect(db)['ok']


def test_recovery_preserves_committed_wal_after_abrupt_process_exit(tmp_path):
    import subprocess,sys
    path=tmp_path/'jobs.sqlite3';corrupt(path)
    code="import sqlite3,os,sys; db=sqlite3.connect(sys.argv[1]); db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA wal_autocheckpoint=0'); db.execute(\"INSERT INTO profiles VALUES('wal-latest','{}')\"); db.commit(); os._exit(0)"
    subprocess.run([sys.executable,'-c',code,str(path)],check=True)
    assert Path(str(path)+'-wal').stat().st_size>0
    backups.before_upgrade(path,recovery=lambda _:True)
    with closing(sqlite3.connect(path)) as db:
        assert inspect(db)['ok']
        assert db.execute("SELECT COUNT(*) FROM profiles WHERE name='wal-latest'").fetchone()[0]==1
    original=next((tmp_path/'backups/recovery-originals').glob('original-*.sqlite3'))
    with closing(sqlite3.connect(original)) as db:
        assert db.execute("SELECT COUNT(*) FROM profiles WHERE name='wal-latest'").fetchone()[0]==1
