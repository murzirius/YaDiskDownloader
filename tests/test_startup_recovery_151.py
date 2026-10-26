"""Startup failures and slow upgrade backups use only synthetic local history."""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from disk_downloader import backups, startup


def history(tmp_path):
    path=tmp_path/'jobs.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY,body BLOB)')
        db.executemany('INSERT INTO fixture(body) VALUES(?)',[(b'synthetic'*2048,) for _ in range(64)])
    return path


def test_upgrade_copy_accepts_progress_beyond_interactive_30s_without_skipping_checks(tmp_path,monkeypatch):
    source=history(tmp_path);before=hashlib.sha256(source.read_bytes()).digest()
    clock={'now':0};original=backups.snapshot;called=[]
    monkeypatch.setattr(backups,'time',SimpleNamespace(monotonic=lambda:clock['now'],sleep=lambda _:None))
    def slow(*args,**kwargs):
        clock['now']+=40;called.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(backups,'snapshot',slow)
    monkeypatch.setattr(backups,'UPGRADE_BACKUP_TIMEOUT',30)
    with pytest.raises(backups.BackupTimeout):backups.before_upgrade(source)
    assert not list((tmp_path/'backups').glob('before-*.sqlite3'))
    monkeypatch.setattr(backups,'UPGRADE_BACKUP_TIMEOUT',600)
    backups.before_upgrade(source)
    copies=list((tmp_path/'backups').glob('before-*.sqlite3'));assert len(copies)==1
    with sqlite3.connect(copies[0]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT COUNT(*) FROM fixture').fetchone()[0]==64
    assert len(called)==2 and hashlib.sha256(source.read_bytes()).digest()==before
    assert not list((tmp_path/'backups').glob('.backup-*'))
    assert backups.BACKUP_TIMEOUT==30 and backups.BACKUP_BUSY_TIMEOUT==5


def test_cancel_upgrade_copy_preserves_history_and_removes_temporary_copy(tmp_path):
    source=history(tmp_path);before=hashlib.sha256(source.read_bytes()).digest();calls=[]
    def cancelled():
        calls.append(1)
        if len(calls)>=4:raise startup.StartupCancelled()
    with pytest.raises(startup.StartupCancelled):backups.before_upgrade(source,checkpoint=cancelled)
    assert hashlib.sha256(source.read_bytes()).digest()==before
    assert not list((tmp_path/'backups').glob('before-*.sqlite3'))
    assert not list((tmp_path/'backups').glob('.backup-*'))


@pytest.mark.parametrize('error,cause',[(PermissionError(13,'PRIVATE-PATH'),'permission'),
 (OSError(28,'PRIVATE-PATH'),'disk_full'),(ImportError('PRIVATE-TOKEN'),'component'),
 (backups.BackupTimeout('PRIVATE-PATH'),'history_timeout'),(RuntimeError('PRIVATE-TOKEN'),'application')])
def test_startup_report_is_actionable_without_private_data(tmp_path,error,cause):
    startup.reset();startup.mark('history')
    try:raise error
    except Exception as exc:detail=startup.failure(exc,state=tmp_path)
    assert detail['report'].is_file() and detail['phase']=='Резервная копия и подготовка истории'
    text=detail['report'].read_text(encoding='utf-8');data=json.loads(text)
    assert data['cause']==cause and data['phase']=='history'
    assert 'PRIVATE' not in text and str(tmp_path) not in text
    assert data['frames'] and all(set(f)=={'file','function','line'} for f in data['frames'])


def test_startup_report_preserves_structured_sqlite_cause(tmp_path):
    try:
        with sqlite3.connect(':memory:') as db:db.execute('SELECT absent_column FROM sqlite_master')
    except sqlite3.Error as error:detail=startup.failure(error,state=tmp_path)
    report=json.loads(detail['report'].read_text(encoding='utf-8'))
    assert report['sqlite_code']=='SQLITE_ERROR' and report['sqlite']['reason']=='missing_column'
    assert 'absent_column' not in detail['report'].read_text(encoding='utf-8')


def test_startup_report_falls_back_when_state_is_not_writable_directory(tmp_path,monkeypatch):
    blocked=tmp_path/'blocked';blocked.write_text('keep',encoding='utf-8')
    monkeypatch.setattr(startup.tempfile,'gettempdir',lambda:str(tmp_path/'temp'))
    detail=startup.failure(PermissionError(13,'private'),state=blocked)
    assert detail['report'].is_relative_to(tmp_path/'temp')
    assert blocked.read_text(encoding='utf-8')=='keep'


def test_startup_report_retention_never_removes_other_files(tmp_path):
    folder=tmp_path/'startup-diagnostics';folder.mkdir();other=folder/'keep.txt';other.write_text('keep')
    for _ in range(8):startup.failure(RuntimeError('private'),state=tmp_path)
    assert len(list(folder.glob('startup-*.txt')))==5 and other.read_text()=='keep'


def test_startup_cancellation_is_reset_for_a_new_launch():
    startup.cancel()
    with pytest.raises(startup.StartupCancelled):startup.checkpoint()
    startup.reset();startup.checkpoint()
