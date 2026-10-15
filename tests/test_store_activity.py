import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from disk_downloader.backups import snapshot
from disk_downloader.store import Store


def test_activity_keeps_wal_open_but_commits_are_visible_and_backup_is_current(tmp_path):
    store = Store(tmp_path/'state.sqlite3')
    wal = Path(store.filename+'-wal')
    with store.activity():
        with store.db() as db:
            assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
            assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            db.execute('INSERT INTO preferences VALUES(?,?)', ('test', 'first'))
        assert wal.is_file() and wal.stat().st_size > 0
        complete = threading.Event()
        errors = []

        def writer():
            try:
                with store.db() as db:
                    db.execute("UPDATE preferences SET value='second' WHERE key='test'")
            except BaseException as exc:
                errors.append(exc)
            finally:
                complete.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            assert complete.wait(10), 'Idle WAL connection blocked another writer'
        finally:
            thread.join(10)
        assert not errors
        with closing(sqlite3.connect(store.filename)) as observer:
            assert observer.execute("SELECT value FROM preferences WHERE key='test'").fetchone()[0] == 'second'
            backup = tmp_path/'backup.sqlite3'
            snapshot(observer, backup)
        with closing(sqlite3.connect(backup)) as saved:
            assert saved.execute("SELECT value FROM preferences WHERE key='test'").fetchone()[0] == 'second'
        with pytest.raises(ValueError):
            with store.db() as db:
                db.execute("UPDATE preferences SET value='uncommitted' WHERE key='test'")
                raise ValueError('rollback')
        assert next(store.rows('SELECT value FROM preferences'))['value'] == 'second'
    assert not wal.exists(), 'The activity left an open connection'


def test_short_transactions_do_not_close_and_recreate_wal_during_activity(tmp_path, monkeypatch):
    store = Store(tmp_path/'state.sqlite3')
    original = sqlite3.connect
    last_closes = []
    wal = Path(store.filename+'-wal')

    class Connection(sqlite3.Connection):
        def close(self):
            had_wal = wal.exists()
            super().close()
            if had_wal and not wal.exists():
                last_closes.append(True)

    def connect(*args, **kwargs):
        return original(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with store.activity():
        for index in range(50):
            with store.db() as db:
                db.execute('INSERT INTO preferences VALUES(?,?)', (str(index), str(index)))
        assert not last_closes
    assert len(last_closes) == 1
    assert len(list(store.rows('SELECT * FROM preferences'))) == 50
