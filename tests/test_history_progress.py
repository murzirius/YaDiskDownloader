from contextlib import closing
import json
import queue
import sqlite3
from types import SimpleNamespace
import pytest
from disk_downloader import backups, desktop, startup
from disk_downloader.history_progress import HistoryProgress
from disk_downloader.store import Store
from test_history_integrity_recovery import corrupt


def test_recovery_reports_real_copy_counts_and_all_post_confirmation_stages(tmp_path):
    path=tmp_path/'jobs.sqlite3';corrupt(path);events=[]
    backups.before_upgrade(path,recovery=lambda _:True,progress=lambda *event:events.append(event))
    stages=[e[0] for e in events]
    assert stages.index('confirm_recovery') < stages.index('save_original') < stages.index('apply_recovery') < stages.index('verify_recovery') < stages.index('history_ready')
    for stage in ('backup_copy','save_original','apply_recovery'):
        copied=[e for e in events if e[0]==stage and e[2] is not None]
        assert copied and copied[-1][1]==copied[-1][2]>0
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT COUNT(*) FROM fixture').fetchone()[0]==64


def test_progress_is_throttled_and_storage_is_constant(monkeypatch):
    import disk_downloader.history_progress as module
    clock={'now':0.0};events=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock['now'])
    reporter=HistoryProgress(observer=lambda *event:events.append(event))
    reporter.begin('verify_recovery')
    for _ in range(10000):reporter.check()
    assert len(events)==1
    clock['now']=.25;reporter.check()
    assert len(events)==2 and events[-1][3]==.25


def test_confirmation_wait_does_not_exhaust_processing_budget(tmp_path,monkeypatch):
    import time
    path=tmp_path/'jobs.sqlite3';corrupt(path)
    clock={'offset':0};real=time.monotonic
    monkeypatch.setattr(backups,'time',SimpleNamespace(monotonic=lambda:real()+clock['offset'],sleep=time.sleep))
    def confirm(_):clock['offset']+=backups.UPGRADE_BACKUP_TIMEOUT+100;return True
    backups.before_upgrade(path,recovery=confirm)
    with closing(sqlite3.connect(path)) as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_display_failure_cannot_abort_durable_copy(tmp_path):
    path=tmp_path/'jobs.sqlite3';corrupt(path)
    def broken(*_):raise RuntimeError('synthetic display unavailable')
    backups.before_upgrade(path,recovery=lambda _:True,progress=broken)
    with closing(sqlite3.connect(path)) as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_application_gets_own_budget_after_heavy_preparation(tmp_path,monkeypatch):
    import os,time
    path=tmp_path/'jobs.sqlite3';corrupt(path)
    clock={'offset':0};real=time.monotonic
    monkeypatch.setattr(backups,'time',SimpleNamespace(monotonic=lambda:real()+clock['offset'],sleep=time.sleep))
    snapshot=backups.snapshot;copy=backups.backup_connection;applied=[]
    def slow_prepare(*args,**kwargs):
        try:return snapshot(*args,**kwargs)
        except backups.HistoryIntegrityError:
            clock['offset']+=backups.UPGRADE_BACKUP_TIMEOUT-10
            raise
    def slow_apply(source,target,*args,**kwargs):
        if any(row[2] and os.path.samefile(row[2],path) for row in target.execute('PRAGMA database_list')):
            clock['offset']+=20;applied.append(True)
        return copy(source,target,*args,**kwargs)
    monkeypatch.setattr(backups,'snapshot',slow_prepare)
    monkeypatch.setattr(backups,'backup_connection',slow_apply)
    backups.before_upgrade(path,recovery=lambda _:True)
    assert applied==[True]
    with closing(sqlite3.connect(path)) as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_sql_observer_cancellation_rolls_back_and_preserves_exception(tmp_path):
    path=tmp_path/'jobs.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE kept(value INTEGER)');db.commit()
        observer=HistoryProgress(checkpoint=lambda:None,observer=lambda *_:None)
        with pytest.raises(startup.StartupCancelled):
            with db,observer.statements(db,'prepare_jobs'):
                db.execute('INSERT INTO kept VALUES(1)')
                observer.checkpoint=lambda:(_ for _ in ()).throw(startup.StartupCancelled())
                db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT SUM(x) FROM n').fetchone()
        assert db.execute('SELECT COUNT(*) FROM kept').fetchone()[0]==0
        assert db.execute('SELECT 1').fetchone()[0]==1


def test_store_reports_setup_and_job_preparation(tmp_path):
    seen=[];store=Store(tmp_path/'jobs.sqlite3',progress=lambda *event:seen.append(event))
    assert {'setup_history','prepare_jobs'} <= {e[0] for e in seen}
    with store.db() as db:assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0


def test_native_progress_names_source_and_never_moves_bar_backwards(monkeypatch):
    window=SimpleNamespace(messages=queue.Queue(),app=None)
    monkeypatch.setattr(desktop,'_window',window)
    desktop.history_progress('save_original',1024**2,2*1024**2,1)
    desktop.history_progress('verify_repaired',None,None,3)
    desktop.history_progress('apply_recovery',2*1024**2,2*1024**2,4)
    queued=[]
    while not window.messages.empty():queued.append(window.messages.get_nowait())
    assert sum(kind=='recovery_notice' for kind,_ in queued)==1
    events=[value for kind,value in queued if kind=='progress']
    assert 'исходную историю' in events[0][0] and '1 / 2 МБ (50%)' in events[0][0]
    assert '3 с' in events[1][0]
    assert [v[1] for v in events]==sorted(v[1] for v in events)
