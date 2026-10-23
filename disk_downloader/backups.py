"""Local, consistent SQLite backups. Never used for public diagnostics."""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .paths import native

MAX_BYTES = 1024 * 1024 * 1024
LOCK = threading.RLock()
BACKUP_TIMEOUT = 30
BACKUP_BUSY_TIMEOUT = 5


class BackupTimeout(TimeoutError):
    """Known safe local-history deadline message, suitable for the interface."""


class BackupBudget:
    def __init__(self, timeout=None, checkpoint=lambda: None):
        self.deadline = time.monotonic() + (BACKUP_TIMEOUT if timeout is None else timeout)
        self.checkpoint = checkpoint
        self.busy_since = None
        self.sql_error = None

    def check(self):
        self.checkpoint()
        if time.monotonic() >= self.deadline:
            raise BackupTimeout('Приложение на ПК не успело сохранить или восстановить историю. Предыдущие данные сохранены. Повторите операцию, когда накопитель освободится.')

    def remaining(self):
        self.check()
        return max(.001, self.deadline-time.monotonic())

    def busy(self, since):
        self.check()
        if time.monotonic()-since >= BACKUP_BUSY_TIMEOUT:
            raise BackupTimeout('История на ПК занята другой операцией. Предыдущие данные сохранены. Подождите и повторите создание или восстановление копии.')

    def progress(self, status, remaining, total):
        # DONE is SQLite's durable success boundary; do not turn a completed
        # connection copy into a false cancellation after its commit.
        if status == sqlite3.SQLITE_DONE:
            return
        now = time.monotonic()
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            self.busy_since = self.busy_since or now
            self.busy(self.busy_since)
        else:
            self.busy_since = None
            self.check()

    def sql_progress(self):
        try:
            self.check()
            return 0
        except BaseException as exc:
            self.sql_error = exc
            return 1


@contextmanager
def backup_lock(budget):
    started = time.monotonic()
    while not LOCK.acquire(timeout=.05):
        budget.busy(started)
    try:
        budget.check()
        yield
    finally:
        LOCK.release()


@contextmanager
def bounded_statements(db, budget):
    previous = db.execute('PRAGMA busy_timeout').fetchone()[0]
    db.execute('PRAGMA busy_timeout=50')
    db.set_progress_handler(budget.sql_progress, 1000)
    admission = getattr(db, 'admission_checkpoint', None)
    if hasattr(db, 'admission_checkpoint'):
        admitted_at = None
        def admission_check():
            nonlocal admitted_at
            admitted_at = admitted_at or time.monotonic()
            budget.busy(admitted_at)
        db.admission_checkpoint = admission_check
    try:
        budget.check()
        yield
    except sqlite3.Error:
        if budget.sql_error is not None:
            raise budget.sql_error from None
        raise
    finally:
        # Remove cancellation before Store commits/rolls back its transaction.
        db.set_progress_handler(None, 0)
        if hasattr(db, 'admission_checkpoint'):
            db.admission_checkpoint = admission
        db.execute('PRAGMA busy_timeout='+str(previous))


def bounded_commit(db, budget):
    started = time.monotonic()
    while True:
        budget.check()
        try:
            db.commit()
            return
        except sqlite3.OperationalError as error:
            code = getattr(error, 'sqlite_errorcode', 0)
            if code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            budget.busy(started)
            time.sleep(min(.05, budget.remaining()))


def backup_connection(source, target, checkpoint=lambda: None, timeout=None):
    """Copy open SQLite connections with bounded busy waits and cancellation."""
    budget = BackupBudget(timeout, checkpoint)
    previous = []
    try:
        for db in (source, target):
            budget.check()
            previous.append((db, db.execute('PRAGMA busy_timeout').fetchone()[0]))
            # Connection timeouts otherwise hide several seconds inside each
            # backup_step before our progress callback can observe the budget.
            db.execute('PRAGMA busy_timeout=50')
        source.backup(target, pages=128, progress=budget.progress, sleep=.05)
    finally:
        for db, value in previous:
            try:
                db.execute('PRAGMA busy_timeout='+str(value))
            except sqlite3.Error:
                pass


def _database_uri(path, mode):
    filename = Path(path).resolve()
    if os.name == 'nt':
        # Percent-encode the native Windows namespace as the URI path,
        # never as a URI authority or an unescaped query string.
        return 'file:' + quote(native(filename), safe='/:') + '?mode=' + mode
    return filename.as_uri() + '?mode=' + mode


def readonly(path):
    return sqlite3.connect(_database_uri(path, 'ro'), uri=True, timeout=5)


def recovery_source(path, timeout=5):
    # A killed Windows process can briefly retain filesystem handles while
    # SQLite recovers its WAL. Retry only this specific truncate I/O failure;
    # never skip recovery, discard journals, or bypass the verified backup.
    for attempt in range(4):
        source = sqlite3.connect(_database_uri(path, 'rw'), uri=True, timeout=timeout)
        try:
            source.execute('SELECT count(*) FROM sqlite_master').fetchone()
            return source
        except sqlite3.Error as error:
            source.close()
            if os.name != 'nt' or getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_IOERR_TRUNCATE or attempt == 3:
                raise
            time.sleep(.1 * 2**attempt)
        except BaseException:
            source.close()
            raise


def _standalone(db, budget):
    """Complete only our private copy; SQLite manages its own WAL/SHM files."""
    budget.check()
    db.execute('PRAGMA synchronous=FULL')
    if db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
        raise ValueError('Резервная копия на ПК не готова для переноса. Прежние данные сохранены.')
    budget.check()


def snapshot(source, destination, checkpoint=lambda: None, timeout=None):
    destination = Path(native(destination))
    budget = BackupBudget(timeout, checkpoint)
    with closing(sqlite3.connect(destination)) as target, bounded_statements(target,budget):
        backup_connection(source, target, budget.check, budget.remaining())
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Резервная копия не прошла проверку')
        _standalone(target,budget)
    budget.check()
    with open(destination, 'r+b') as stream:
        os.fsync(stream.fileno())


def before_upgrade(filename):
    filename = Path(native(filename))
    if not filename.is_file():
        return
    folder = filename.parent / 'backups'
    target = folder / ('before-' + __version__ + '.sqlite3')
    budget = BackupBudget()
    with backup_lock(budget):
        if target.is_file():
            return
        folder.mkdir(exist_ok=True)
        temporary = folder / ('.backup-' + uuid.uuid4().hex)
        try:
            # Crash recovery may need to repair/truncate WAL or a hot journal.
            # Open the existing local database writable before taking the
            # pre-migration snapshot; mode=rw must never create a missing file.
            with closing(recovery_source(filename, timeout=min(5, budget.remaining()))) as source:
                snapshot(source, temporary, budget.check, budget.remaining())
            os.replace(temporary, target)
            automatic=[file for file in folder.glob('before-*.sqlite3') if not file.is_symlink() and re.fullmatch(r'before-\d+\.\d+\.\d+\.sqlite3',file.name)]
            for old in sorted(automatic,key=lambda file:file.stat().st_mtime,reverse=True)[5:]:
                try: old.unlink()
                except OSError: pass
        finally:
            temporary.unlink(missing_ok=True)


class Backups:
    def __init__(self, store):
        self.store = store
        self.folder = Path(native(Path(store.filename).parent / 'backups'))
        self.folder.mkdir(exist_ok=True)

    def list(self):
        return [{"name": f.name, "bytes": f.stat().st_size, "modified": f.stat().st_mtime}
                for f in sorted(self.folder.glob('*.sqlite3'), key=lambda f: f.stat().st_mtime, reverse=True)
                if not f.is_symlink()]

    def path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'(?:backup-[0-9TZ-]+-[a-f0-9]{8}|before-\d+\.\d+\.\d+)\.sqlite3', name):
            raise ValueError('Некорректное имя резервной копии')
        file = self.folder / name
        if file.is_symlink() or not file.is_file():
            raise ValueError('Резервная копия не найдена')
        return file

    def create(self, checkpoint=lambda: None, timeout=None, *, prune=True):
        budget = BackupBudget(timeout, checkpoint)
        with backup_lock(budget):
            name = 'backup-' + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ') + '-' + uuid.uuid4().hex[:8] + '.sqlite3'
            target = self.folder / name
            temporary = self.folder / ('.backup-' + uuid.uuid4().hex)
            try:
                with closing(readonly(self.store.filename)) as source:
                    snapshot(source, temporary, budget.check, budget.remaining())
                budget.check()
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            if prune:
                self.prune()
            return name

    def prune(self):
        # Retention runs after a complete publication, never while a restore
        # might still be cancelled and need all previous recovery options.
        try:
            ordinary = [f for f in self.list() if f['name'].startswith('backup-')]
            for old in ordinary[10:]:
                try: self.path(old['name']).unlink()
                except (OSError, ValueError): pass
        except OSError:
            pass

    def import_file(self, stream):
        temporary = self.folder / ('.backup-' + uuid.uuid4().hex)
        try:
            count = 0
            with temporary.open('xb') as output:
                while chunk := stream.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise ValueError('Резервная копия слишком большая')
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            budget = BackupBudget()
            # This is our exclusive temporary copy, never the input stream or
            # a legacy source. Validate before making it a standalone database.
            with closing(sqlite3.connect(_database_uri(temporary,'rw'),uri=True,timeout=.05)) as source, bounded_statements(source,budget):
                self.validate(source,budget.check)
                _standalone(source,budget)
            name = 'backup-' + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ') + '-' + uuid.uuid4().hex[:8] + '.sqlite3'
            os.replace(temporary, self.folder / name)
            return name
        except (sqlite3.Error, json.JSONDecodeError, TypeError, KeyError):
            raise ValueError('Некорректная или повреждённая резервная копия') from None
        finally:
            temporary.unlink(missing_ok=True)

    def validate(self, source, checkpoint=lambda: None):
        checkpoint()
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Резервная копия повреждена')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'jobs', 'entries', 'names', 'artifacts', 'profiles', 'preferences'}.issubset(tables):
            raise ValueError('Это не резервная копия YaDiskDownloader')
        encoding = {'UTF-8':'utf-8','UTF-16le':'utf-16-le','UTF-16be':'utf-16-be'}[source.execute('PRAGMA encoding').fetchone()[0]]
        for row in source.execute('SELECT CAST(settings AS BLOB),root FROM jobs'):
            checkpoint()
            try:
                if not isinstance(json.loads(row[0].decode(encoding)), dict) or not Path(row[1]).is_absolute():
                    raise ValueError()
            except (ValueError, TypeError, AttributeError, RecursionError):
                raise ValueError('В резервной копии повреждены настройки задания. Текущая история не изменена. Выберите другую копию, а этот файл сохраните для восстановления.') from None
        for row in source.execute('SELECT CAST(settings AS BLOB) FROM profiles'):
            checkpoint()
            try:
                if not isinstance(json.loads(row[0].decode(encoding)), dict):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError, RecursionError):
                raise ValueError('В резервной копии повреждены настройки профиля. Текущая история не изменена. Выберите другую копию, а этот файл сохраните для восстановления.') from None
        return tables

    def restore(self, name, checkpoint=lambda: None, timeout=None):
        # Copy data into our known schema, never execute schema supplied by a backup.
        budget = BackupBudget(timeout, checkpoint)
        with backup_lock(budget), closing(readonly(self.path(name))) as source, bounded_statements(source,budget):
            tables = self.validate(source, budget.check)
            safety = self.create(budget.check, budget.remaining(), prune=False)
            with self.store.db() as target, bounded_statements(target,budget):
                own = [r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in own:
                    budget.check()
                    target.execute('DELETE FROM "' + table + '"')
                for table in own:
                    budget.check()
                    if table not in tables or table in ('run_selection', 'duplicate_candidates', 'duplicate_moves', 'verification_cache', 'verification_results', 'partial_ownership', 'pending_publications', 'service_outputs', 'service_output_signatures', 'pending_service_outputs', 'temporary_files'):
                        continue
                    available = {r[1] for r in source.execute('PRAGMA table_info("' + table + '")')}
                    columns = [r[1] for r in target.execute('PRAGMA table_info("' + table + '")') if r[1] in available]
                    if not columns:
                        continue
                    names = ','.join('"' + c + '"' for c in columns)
                    target.executemany('INSERT INTO "' + table + '" (' + names + ') VALUES (' + ','.join('?' for _ in columns) + ')', source.execute('SELECT ' + names + ' FROM "' + table + '"'))
                target.execute("UPDATE jobs SET phase='interrupted',error='Восстановлено из резервной копии. Подключите Диск и продолжите.' WHERE phase IN ('scanning','preparing','downloading','packing','paused','pausing','stopping')")
                target.execute("UPDATE entries SET status='pending' WHERE status='running'")
                target.execute("UPDATE job_queue SET state='waiting' WHERE state='running'")
                target.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','true')")
                # Commit while the short SQLite timeout and operation budget
                # still apply; Store.db's final empty commit remains harmless.
                bounded_commit(target,budget)
            self.prune()
            return safety
