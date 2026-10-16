import sqlite3
import threading
import time

from conftest import test_timeout
from disk_downloader.store import Store
from disk_downloader.sqlite_writes import WriteConnection


def test_slow_full_commits_do_not_starve_other_download_writers(tmp_path, monkeypatch):
    store = Store(tmp_path/'history.sqlite3')
    other = Store(store.filename)
    with store.db() as db:
        db.execute("INSERT INTO preferences VALUES('counter','0')")
    connect = sqlite3.connect
    class Slow(WriteConnection):
        def commit(self):
            if self.in_transaction:
                time.sleep(.02)  # Hold SQLite's real write lock during slow I/O.
            super().commit()
    def slow_connect(*args, **kwargs):
        kwargs.update(factory=Slow,timeout=.005)
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3,'connect',slow_connect)
    errors = []
    ready = threading.Barrier(8)
    def writer(number):
        try:
            ready.wait(test_timeout(5))
            for _ in range(12):
                with (store if number%2 else other).db() as db:
                    assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
                    # Exercise both connection and cursor write APIs.
                    target = db.cursor() if number%2 else db
                    target.execute("UPDATE preferences SET value=CAST(value AS INTEGER)+1 WHERE key='counter'")
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=writer,args=(n,)) for n in range(8)]
    with store.activity():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(test_timeout(15))
        assert not any(t.is_alive() for t in threads), 'A writer was starved'
        assert not errors, errors
        assert next(store.rows("SELECT value FROM preferences WHERE key='counter'"))['value'] == '96'


def test_queued_writer_does_not_lock_out_wal_reader(tmp_path):
    from disk_downloader.sqlite_support import safe_wal
    import pytest
    if not safe_wal():
        pytest.skip('Read/write overlap requires patched WAL; legacy mode has its own snapshot test')
    store = Store(tmp_path/'history.sqlite3')
    with store.db() as db:
        db.execute("INSERT INTO preferences VALUES('counter','before')")
    read = threading.Event()
    errors = []
    def reader():
        try:
            assert next(store.rows('SELECT value FROM preferences'))['value'] == 'before'
        except Exception as exc:
            errors.append(exc)
        finally:
            read.set()
    with store.db() as db:
        db.execute("UPDATE preferences SET value='after'")
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            assert read.wait(test_timeout(3)), 'A read waited for the writer gate'
        finally:
            thread.join(test_timeout(3))
    assert not errors
