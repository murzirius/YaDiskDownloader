"""Classify SQLite integrity failures without retaining row data or messages."""
from collections import Counter
import re
import sqlite3


class HistoryIntegrityError(ValueError):
    def __init__(self, details):
        self.integrity = details
        super().__init__('История заданий не прошла проверку SQLite. Исходная история сохранена; требуется восстановление.')


def inspect(db):
    try:
        rows = db.execute('PRAGMA integrity_check(100)').fetchall()
    except sqlite3.Error as exc:
        code = getattr(exc, 'sqlite_errorcode', 0)
        if code & 255 not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise
        return {'ok': False, 'issues': {'sqlite_corrupt': 1}, 'index_candidate': False,
                'sqlite_code': getattr(exc, 'sqlite_errorname', 'SQLITE_CORRUPT')}
    if len(rows) == 1 and rows[0][0] in ('ok', b'ok'):
        return {'ok': True, 'issues': {}, 'index_candidate': False}
    patterns = (
        (r'row \d+ missing from index .+', 'index_missing'),
        (r'wrong # of entries in index .+', 'index_count'),
        (r'non-unique entry in index .+', 'index_unique'),
        (r'NULL value in .+', 'null_value'),
        (r'CHECK constraint failed in .+', 'check_constraint'),
        (r'NUMERIC value in .+', 'column_type'),
    )
    issues = Counter()
    for row in rows:
        text = str(row[0])[:4096]
        kind = next((kind for pattern, kind in patterns if re.fullmatch(pattern, text)), 'structure_or_other')
        issues[kind] += 1
    return {'ok': False, 'issues': dict(issues), 'reported': len(rows),
            'limit_reached': len(rows) >= 100,
            'index_candidate': bool(issues) and set(issues) <= {'index_missing', 'index_count', 'index_unique'}}


def require(db):
    details = inspect(db)
    if not details['ok']:
        raise HistoryIntegrityError(details)
    return details
