import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from .paths import remote_path,relative_parts,PathError

from .paths import forget_temporary, publish, replace_owned, reserve_output, safe_join, temporary_file


FORMAT = "disk-local-import-v1"


def find_import(store, root, exclude_job=None):
    # Resolve only the chosen destination, never unrelated old NAS roots.
    normalized = os.path.normcase(str(Path(root).resolve()))
    lexical = os.path.normcase(os.path.abspath(root))
    for row in store.rows("SELECT id,root FROM jobs ORDER BY started DESC"):
        if row['id'] == exclude_job:
            continue
        same_name = Path(root).name == "Disk-" + row["id"][:12]
        saved = os.path.normcase(os.path.abspath(row['root']))
        if saved in (lexical, normalized) or same_name:
            job = store.job(row["id"])
            if job['settings'].get('_inherit_source'):
                continue
            return {"job": job, "store": store, "id": job["id"]}
    manifests = []
    for filename in Path(root).glob(".disk-import*.sqlite3"):
        try:
            safe_join(root, filename.name)
            manifests.append((filename.stat().st_mtime, filename))
        except (OSError, ValueError):
            continue
    for _, filename in sorted(manifests, key=lambda item: item[0], reverse=True):
        try:
            return read_import(root, filename.name)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            continue
    return None


def read_import(root, local):
    """Open the exact portable inventory selected when this job was created."""
    # Keep the validated, ordinary path for URI encoding on Python 3.11.
    # safe_join returns a Windows device path whose URI authority is rejected
    # by SQLite on that runtime. Path validation still happens before opening.
    safe_join(root, local)
    filename = Path(root) / local
    with closing(sqlite3.connect(filename.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        meta = dict(db.execute('SELECT key,value FROM import_meta'))
        if meta.get('format') != FORMAT:
            raise ValueError('Локальная опись загрузки имеет неизвестный формат. Выберите прежнюю папку результатов заново.')
        settings = json.loads(meta['settings'])
        if not isinstance(settings, dict) or not isinstance(settings.get('paths'), list):
            raise ValueError('Локальная опись загрузки повреждена. Выберите прежнюю папку результатов заново.')
        return {'job': {'settings': settings, 'service': meta['service'], 'generation': int(meta['generation'])},
                'file': filename, 'inventory_available': bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='inventory'").fetchone())}


def inherit(store, job_id, previous, checkpoint=lambda: None, progress=lambda count: None, details=None):
    """Import reusable file metadata; filesystem verification happens once later.

    Never resolve history paths here: on SMB each safe_join performs several
    network round trips. Names and the comparison baseline are database data,
    not files to inspect. Output names remain reserved to prevent treating a
    generated report as a downloaded original; their content is never read.
    """
    root_length = len(os.path.abspath(store.job(job_id)['root']))
    same = bool(previous.get('store'))
    filename = previous['store'].filename if same else previous['file']
    with closing(sqlite3.connect(Path(filename).resolve().as_uri()+'?mode=ro',uri=True)) as source:
        available = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        where = ' WHERE job=?' if same else ''
        args = (previous['id'],) if same else ()
        inventory = 'entries' if same else 'inventory'
        if details: details(0, None, 'metadata')

        def records(sql, parameters):
            if same and not previous['store'].wal:
                # Legacy CMD SQLite uses rollback journaling: release the read
                # lock before writing batches into another job of the same DB.
                yield from (tuple(row.values()) for row in previous['store'].rows(sql,parameters))
            else:
                yield from source.execute(sql,parameters)

        def copy(table, sql, parameters, remote=None, local=None):
            batch = []
            for record in records(sql, parameters):
                checkpoint()
                row = tuple(record)
                if remote is not None: remote_path(row[remote])
                if local is not None:
                    relative_parts(row[local])
                    if root_length+1+len(row[local])>30000:
                        raise PathError('Путь превышает предел файловой системы')
                batch.append((job_id, *row))
                if len(batch) == 200:
                    write(table, batch)
                    batch.clear()
            if batch: write(table, batch)

        def write(table, batch):
            with store.db() as target:
                target.executemany('INSERT OR IGNORE INTO '+table+' VALUES('+','.join('?' for _ in batch[0])+')',batch)

        # Retain change reports and collision-safe names without filesystem IO.
        if inventory in available:
            copy('baseline', 'SELECT path,kind,meta FROM '+inventory+where, args, remote=0)
        if same:
            copy('baseline', "SELECT b.path,b.kind,b.meta FROM baseline b LEFT JOIN changes c ON c.job=b.job AND c.path=b.path WHERE b.job=? AND (c.status='unknown' OR ?=0)",
                 (previous['id'],previous['job']['scan_complete']), remote=0)
        if 'names' in available:
            copy('names', 'SELECT identity,local,folded FROM names'+where+(' AND ' if where else ' WHERE ')+
                 "(identity LIKE 'disk:/%' OR identity LIKE '@preview:disk:/%' OR identity LIKE '@output:%' OR identity='@service')", args, local=1)
        if 'partials' in available:
            copy('partials','SELECT path,type,local,fingerprint,size,sha256,etag,total FROM partials'+where,args,remote=0,local=2)

        if 'artifacts' not in available:
            if details: details(0, 0, 'files')
            return
        # One progress item per source file, even when both copies are present.
        total = source.execute('SELECT COUNT(DISTINCT path) FROM artifacts'+where,args).fetchone()[0]
        if details: details(0, total, 'files')
        cached = same and 'verification_cache' in available
        join = ' LEFT JOIN verification_cache v ON v.job=a.job AND v.local=a.local' if cached else ''
        sql = 'SELECT a.path,a.type,a.local,a.size,a.sha256,a.fingerprint,a.preview_size,'+('v.signature' if cached else 'NULL')+' FROM artifacts a'+join+(' WHERE a.job=?' if same else '')+' ORDER BY a.path,a.type'
        batch, cache_batch = [], []
        done = 0
        last_path = None

        def flush():
            if not batch: return
            with store.db() as target:
                target.executemany('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?)', batch)
                target.executemany('INSERT OR IGNORE INTO verification_cache VALUES(?,?,?)', cache_batch)
            batch.clear()
            cache_batch.clear()
            progress(done)
            if details: details(done,total,'files')

        for record in records(sql,args):
            checkpoint()
            path,kind,local,size,sha,stamp,preview_size,signature = record
            remote_path(path)
            relative_parts(local)
            if root_length+1+len(local)>30000:
                raise PathError('Путь превышает предел файловой системы')
            if kind not in ('original','preview'):
                raise ValueError('Локальная опись содержит неизвестный тип копии. Выберите прежнюю папку результатов заново.')
            if path != last_path:
                if len(batch) >= 200: flush()
                done += 1
                last_path = path
            batch.append((job_id,path,kind,local,size,sha,stamp,preview_size))
            # A portable manifest may never authorize skipping content hashing.
            if signature is not None and kind in ('original','preview'):
                cache_batch.append((job_id,local,signature))
        flush()


def save_import(store, job_id, checkpoint=lambda: None):
    job = store.job(job_id)
    local = job["settings"].get("manifest_local")
    owned = False
    if local:
        filename = safe_join(job["root"], local)
        if os.path.isfile(filename):
            try:
                with closing(sqlite3.connect((Path(job["root"]) / local).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    owned = db.execute("SELECT value FROM import_meta WHERE key='format'").fetchone() == (FORMAT,)
            except sqlite3.Error:
                pass
        else:
            owned = True
    if not local or not owned:
        local = reserve_output(store, job, ".disk-import.sqlite3")
        job["settings"]["manifest_local"] = local
        store.update_job(job_id, settings=json.dumps(job["settings"], ensure_ascii=False))
    temp = temporary_file(job["root"], store, 'manifest')
    def checked(rows):
        for row in rows:
            checkpoint()
            yield row
    try:
        with closing(sqlite3.connect(temp)) as db:
            db.executescript("CREATE TABLE import_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE names(identity TEXT PRIMARY KEY,local TEXT,folded TEXT); CREATE TABLE artifacts(path TEXT,type TEXT,local TEXT,size INTEGER,sha256 TEXT,fingerprint TEXT,preview_size TEXT,PRIMARY KEY(path,type)); CREATE TABLE partials(path TEXT,type TEXT,local TEXT,fingerprint TEXT,size INTEGER,sha256 TEXT,etag TEXT,total INTEGER,PRIMARY KEY(path,type)); CREATE TABLE inventory(path TEXT PRIMARY KEY,kind TEXT,meta TEXT);")
            db.execute("CREATE TABLE verification_cache(local TEXT PRIMARY KEY,signature TEXT NOT NULL)")
            db.executemany("INSERT INTO import_meta VALUES(?,?)", [("format", FORMAT), ("settings", json.dumps(job["settings"], ensure_ascii=False)), ("service", job["service"]), ("generation", str(job["generation"]))])
            with store.db() as source:
                db.executemany("INSERT INTO verification_cache VALUES(?,?)", checked(source.execute("SELECT local,signature FROM verification_cache WHERE job=?", (job_id,))))
                db.executemany("INSERT INTO inventory VALUES(?,?,?)", checked(source.execute("SELECT path,kind,meta FROM entries WHERE job=?", (job_id,))))
                db.executemany("INSERT OR IGNORE INTO inventory VALUES(?,?,?)", checked(source.execute("SELECT b.path,b.kind,b.meta FROM baseline b LEFT JOIN changes c ON c.job=b.job AND c.path=b.path WHERE b.job=? AND (c.status='unknown' OR ?=0)", (job_id, job["scan_complete"]))))
                db.executemany("INSERT INTO names VALUES(?,?,?)", checked(source.execute("SELECT identity,local,folded FROM names WHERE job=?", (job_id,))))
                db.executemany("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", checked(source.execute("SELECT path,type,local,size,sha256,fingerprint,preview_size FROM artifacts WHERE job=?", (job_id,))))
                db.executemany("INSERT INTO partials VALUES(?,?,?,?,?,?,?,?)", checked(source.execute("SELECT path,type,local,fingerprint,size,sha256,etag,total FROM partials WHERE job=?", (job_id,))))
            checkpoint()
            db.commit()
        checkpoint()
        target = safe_join(job["root"], local)
        if owned and os.path.exists(target):
            replace_owned(temp, target)
        else:
            publish(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
        forget_temporary(store,job['root'],temp)
