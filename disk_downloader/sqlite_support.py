"""SQLite WAL-reset fix: https://www.sqlite.org/wal.html#walresetbug ."""
import sqlite3
import sys


def safe_wal(version=None):
    version = tuple(version or sqlite3.sqlite_version_info)
    return (version >= (3, 51, 3) or
            (3, 50, 7) <= version < (3, 51, 0) or
            (3, 44, 6) <= version < (3, 45, 0))


def require_safe():
    if not safe_wal():
        raise RuntimeError('SQLite ' + sqlite3.sqlite_version +
                           ' has the WAL-reset bug. Build with SQLite 3.51.3 or newer.')


def journal_mode():
    if getattr(sys, 'frozen', False):
        require_safe()
    # Legacy source/CMD runtimes use the rollback journal. FULL remains enabled.
    return 'WAL' if safe_wal() else 'DELETE'
