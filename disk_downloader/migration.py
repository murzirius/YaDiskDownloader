import json
import sqlite3
import os
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from .store import Store
from .backups import backup_connection


TABLES = ("entries", "names", "artifacts", "partials", "baseline", "changes", "scan_errors")


@contextmanager
def backup_snapshot(source, folder):
    descriptor, name = tempfile.mkstemp(prefix=".migration-", suffix=".sqlite3", dir=folder)
    os.close(descriptor)
    filename = Path(name).resolve()
    if filename.parent != Path(folder).resolve():
        raise ValueError("Некорректная папка переноса")
    try:
        with closing(sqlite3.connect(filename)) as snapshot:
            backup_connection(source,snapshot)
            snapshot.row_factory = sqlite3.Row
            yield snapshot
    finally:
        filename.unlink(missing_ok=True)


@contextmanager
def legacy_lock(folder):
    stream = (Path(folder) / "server.lock").open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def candidates(program_dir):
    program_dir = Path(program_dir).resolve()
    locations = [program_dir / ".state", program_dir / "disk-downloader" / ".state", program_dir.parent / "disk-downloader" / ".state"]
    try:
        for path in program_dir.parent.iterdir():
            if path.is_dir() and path.name.lower().startswith(("disk-downloader", "yadiskdownloader")):
                locations.append(path / ".state")
    except OSError:
        pass
    return list(dict.fromkeys(path for path in locations if (path / "jobs.sqlite3").is_file()))[:50]


def migrate(target, sources, checkpoint=lambda: None, recovery=None, progress=None):
    target = Path(target).resolve()
    store = Store(target / "jobs.sqlite3", checkpoint=checkpoint, recovery=recovery, progress=progress)
    from .history_progress import HistoryProgress
    observation = HistoryProgress(checkpoint, progress)
    total, failures = 0, 0
    for folder in sources:
        checkpoint()
        filename = (Path(folder) / "jobs.sqlite3").resolve()
        if filename == Path(store.filename).resolve():
            continue
        try:
            with legacy_lock(filename.parent), closing(sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True, timeout=3)) as source:
                source.row_factory = sqlite3.Row
                tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"jobs", "entries", "names", "artifacts"}.issubset(tables):
                    continue
                with store.db() as db:
                    previous = db.execute("SELECT value FROM preferences WHERE key=?", ("migration:" + str(filename),)).fetchone()
                    info = filename.stat()
                    wal = Path(str(filename) + "-wal")
                    wal_info = wal.stat() if wal.exists() else None
                    stamp = json.dumps([info.st_size, info.st_mtime_ns, wal_info.st_size if wal_info else 0, wal_info.st_mtime_ns if wal_info else 0])
                    if previous and previous[0] == stamp:
                        continue
                with backup_snapshot(source, target) as snapshot:
                    with store.db() as db, observation.statements(db, 'migrate_legacy'):
                        imported = []
                        columns = [row[1] for row in db.execute("PRAGMA table_info(jobs)") if row[1] in {r[1] for r in snapshot.execute("PRAGMA table_info(jobs)")}]
                        for job in snapshot.execute("SELECT * FROM jobs"):
                            if db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
                                continue
                            settings = json.loads(job["settings"])
                            if not isinstance(settings, dict) or not Path(job["root"]).is_absolute():
                                raise ValueError("Некорректное старое задание")
                            db.execute('INSERT INTO jobs (' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(job[key] for key in columns))
                            db.execute('INSERT OR IGNORE INTO journal_jobs VALUES(?,?)', (job['id'],job['root']))
                            imported.append(job["id"])
                        for table in TABLES:
                            if table not in tables:
                                continue
                            available = {row[1] for row in snapshot.execute('PRAGMA table_info(' + table + ')')}
                            columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')') if row[1] in available]
                            sql = 'INSERT OR IGNORE INTO ' + table + ' (' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')'
                            for job_id in imported:
                                db.executemany(sql, (tuple(row[key] for key in columns) for row in snapshot.execute('SELECT * FROM ' + table + ' WHERE job=?', (job_id,))))
                        if "journal_events" in tables:
                            for job_id in imported:
                                db.executemany("INSERT INTO journal_events(job,time,category,level,message,path) VALUES(?,?,?,?,?,?)", snapshot.execute("SELECT job,time,category,level,message,path FROM journal_events WHERE job=? ORDER BY id", (job_id,)))
                        if "profiles" in tables:
                            for profile in snapshot.execute("SELECT name,settings FROM profiles"):
                                db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?)", tuple(profile))
                        if "preferences" in tables:
                            for pref in snapshot.execute("SELECT key,value FROM preferences WHERE key='updates'"):
                                db.execute("INSERT OR IGNORE INTO preferences VALUES(?,?)", tuple(pref))
                        db.execute("UPDATE jobs SET phase='interrupted',error='Задание перенесено. Подключите Диск и продолжите.' WHERE phase IN ('scanning','preparing','downloading','packing','pausing','stopping')")
                        db.execute("UPDATE entries SET status='pending' WHERE status='running'")
                        db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", ("migration:" + str(filename), stamp))
                    total += len(imported)
        except (OSError, sqlite3.Error, ValueError, KeyError, IndexError, TypeError):
            failures += 1
    return {"jobs": total, "failed_sources": failures}
