"""Local, consistent SQLite backups. Never used for public diagnostics."""
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

MAX_BYTES = 1024 * 1024 * 1024
LOCK = threading.RLock()


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def snapshot(source, destination):
    with closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Резервная копия не прошла проверку')
    with open(destination, 'r+b') as stream:
        os.fsync(stream.fileno())


def before_upgrade(filename):
    if not filename.is_file():
        return
    folder = filename.parent / 'backups'
    target = folder / ('before-' + __version__ + '.sqlite3')
    with LOCK:
        if target.is_file():
            return
        folder.mkdir(exist_ok=True)
        temporary = folder / ('.backup-' + uuid.uuid4().hex)
        try:
            with closing(readonly(filename)) as source:
                snapshot(source, temporary)
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
        self.folder = Path(store.filename).parent / 'backups'
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

    def create(self):
        with LOCK:
            name = 'backup-' + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ') + '-' + uuid.uuid4().hex[:8] + '.sqlite3'
            target = self.folder / name
            temporary = self.folder / ('.backup-' + uuid.uuid4().hex)
            try:
                with closing(readonly(self.store.filename)) as source:
                    snapshot(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            ordinary = [f for f in self.list() if f['name'].startswith('backup-')]
            for old in ordinary[10:]:
                try: self.path(old['name']).unlink()
                except OSError: pass
            return name

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
            with closing(readonly(temporary)) as source:
                self.validate(source)
            name = 'backup-' + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ') + '-' + uuid.uuid4().hex[:8] + '.sqlite3'
            os.replace(temporary, self.folder / name)
            return name
        except (sqlite3.Error, json.JSONDecodeError, TypeError, KeyError):
            raise ValueError('Некорректная или повреждённая резервная копия') from None
        finally:
            temporary.unlink(missing_ok=True)

    def validate(self, source):
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Резервная копия повреждена')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'jobs', 'entries', 'names', 'artifacts', 'profiles', 'preferences'}.issubset(tables):
            raise ValueError('Это не резервная копия YaDiskDownloader')
        for row in source.execute('SELECT settings,root FROM jobs'):
            if not isinstance(json.loads(row[0]), dict) or not Path(row[1]).is_absolute():
                raise ValueError('Некорректное задание в резервной копии')
        return tables

    def restore(self, name):
        # Copy data into our known schema, never execute schema supplied by a backup.
        with LOCK, closing(readonly(self.path(name))) as source:
            tables = self.validate(source)
            safety = self.create()
            with self.store.db() as target:
                own = [r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in own:
                    target.execute('DELETE FROM "' + table + '"')
                for table in own:
                    if table not in tables or table in ('duplicate_candidates', 'duplicate_moves', 'verification_cache', 'verification_results', 'partial_ownership', 'pending_publications', 'service_outputs', 'service_output_signatures', 'pending_service_outputs', 'temporary_files'):
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
            return safety
