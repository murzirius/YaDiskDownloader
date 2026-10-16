import sqlite3
import threading

import pytest
from disk_downloader.store import Store

def test_session_preserves_commits_rollbacks_and_nested_reads(tmp_path):
    store=Store(tmp_path/'state.sqlite3'); connections=[]
    with store.session():
        with store.db() as db:
            connections.append(db)
            assert db.execute('PRAGMA synchronous').fetchone()[0]==2
            db.execute("INSERT INTO preferences VALUES('first','committed')")
        with sqlite3.connect(store.filename) as independent:
            assert independent.execute("SELECT value FROM preferences WHERE key='first'").fetchone()[0]=='committed'
        with pytest.raises(RuntimeError):
            with store.db() as db:
                connections.append(db)
                db.execute("INSERT INTO preferences VALUES('rollback','must-not-survive')")
                raise RuntimeError('abort this transaction')
        with store.db() as outer:
            connections.append(outer)
            assert not outer.execute("SELECT 1 FROM preferences WHERE key='rollback'").fetchone()
            with store.db() as inner:
                assert inner is not outer
                assert inner.execute('PRAGMA foreign_keys').fetchone()[0]==1
    assert len({id(item) for item in connections})==1
    with pytest.raises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')


def test_idle_session_releases_writer_admission(tmp_path):
    store=Store(tmp_path/'state.sqlite3');finished=threading.Event();errors=[]
    def other_writer():
        try:
            with store.db() as db:db.execute("INSERT INTO preferences VALUES('other','ok')")
        except BaseException as exc:errors.append(exc)
        finally:finished.set()
    with store.session():
        with store.db() as db:db.execute("INSERT INTO preferences VALUES('first','ok')")
        thread=threading.Thread(target=other_writer);thread.start()
        try:assert finished.wait(5), 'Idle reusable connection retained the write gate'
        finally:thread.join(10)
    assert not errors


@pytest.mark.parametrize('failure',['commit','rollback'])
def test_session_discards_a_failed_sqlite_connection(tmp_path,monkeypatch,failure):
    from disk_downloader.sqlite_writes import WriteConnection
    store=Store(tmp_path/'state.sqlite3')
    original=getattr(WriteConnection,failure)
    failed=[]
    def fail_once(connection):
        if not failed:
            failed.append(connection)
            raise sqlite3.OperationalError('simulated I/O failure')
        return original(connection)
    monkeypatch.setattr(WriteConnection,failure,fail_once)
    with store.session():
        with pytest.raises(sqlite3.OperationalError):
            with store.db() as db:
                db.execute("INSERT INTO preferences VALUES('bad','uncommitted')")
                if failure=='rollback':raise ValueError('force rollback')
        assert store.write_gate.owner is None
        with store.db() as db:
            assert db is not failed[0]
            assert not db.execute("SELECT 1 FROM preferences WHERE key='bad'").fetchone()
            db.execute("INSERT INTO preferences VALUES('good','ok')")
