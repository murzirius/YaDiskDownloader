"""SQLite WAL-reset fix: https://www.sqlite.org/wal.html#walresetbug ."""
import sqlite3
import sys


def error_details(exc):
    """Classify failures without exporting SQLite text containing private data."""
    text = str(exc).lower()
    reason, message = 'unknown', 'Программа на ПК не смогла прочитать или сохранить историю задания. Точная причина пока неизвестна.'
    for prefix, key, description in (
        ('invalid uri authority:', 'uri_authority', 'Программа на ПК неправильно обработала путь к файлу истории и не смогла его открыть.'),
        ('no such table:', 'missing_table', 'Программа на ПК не нашла нужный раздел в сохранённой истории или описи прежней загрузки.'),
        ('no such column:', 'missing_column', 'Программа на ПК не нашла нужные сведения в истории или описи прежней загрузки.'),
        ('ambiguous column name:', 'ambiguous_column', 'В программе на ПК возникла ошибка при чтении истории задания.'),
        ('near ', 'sql_syntax', 'В программе на ПК возникла ошибка при обращении к истории задания.'),
        ('malformed json', 'invalid_json', 'Программа на ПК не смогла прочитать часть сохранённых сведений о файлах.'),
        ('nested write connections are not supported', 'nested_write', 'В программе на ПК возникла ошибка при сохранении истории задания.'),
    ):
        if text.startswith(prefix):
            reason, message = key, description
            break
    code = getattr(exc, 'sqlite_errorcode', None)
    base = code & 255 if isinstance(code, int) else None
    if base in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        reason, message = 'busy', 'История заданий на ПК занята другой операцией. Подождите и нажмите «Продолжить».'
    elif base in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        reason, message = 'corrupt', 'Программа на ПК не смогла прочитать файл истории или описи: SQLite сообщает о повреждённом или неподходящем формате. Сами скачанные файлы требуют отдельной проверки.'
    elif base in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY):
        reason, message = 'storage', 'Программа на ПК не смогла прочитать или записать историю. Проверьте подключение накопителя и доступ к папке.'
    elif base == sqlite3.SQLITE_FULL:
        reason, message = 'full', 'На накопителе ПК с историей заданий закончилось свободное место. Освободите место и нажмите «Продолжить».'
    return {'code':getattr(exc, 'sqlite_errorname', 'SQLITE_ERROR'), 'reason':reason, 'message':message}


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
