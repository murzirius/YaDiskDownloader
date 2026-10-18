import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from .paths import remote_path

from .paths import forget_temporary, publish, replace_owned, reserve_output, safe_join, temporary_file


FORMAT = "disk-local-import-v1"


def find_import(store, root, exclude_job=None):
    normalized = os.path.normcase(str(Path(root).resolve()))
    for row in store.rows("SELECT id,root FROM jobs ORDER BY started DESC"):
        if row['id'] == exclude_job:
            continue
        same_name = Path(root).name == "Disk-" + row["id"][:12]
        if os.path.normcase(str(Path(row["root"]).resolve())) == normalized or same_name:
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
            safe_join(root, filename.name)
            with closing(sqlite3.connect(filename.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                meta = dict(db.execute("SELECT key,value FROM import_meta"))
                if meta.get("format") == FORMAT:
                    settings = json.loads(meta["settings"])
                    if not isinstance(settings, dict) or not isinstance(settings.get("paths"), list):
                        continue
                    return {"job": {"settings": settings, "service": meta["service"], "generation": int(meta["generation"])}, "file": filename, "inventory_available": bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='inventory'").fetchone())}
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            continue
    return None


def inherit(store, job_id, previous, checkpoint=lambda: None, progress=lambda count: None):
    """Copy in bounded, restartable batches without holding a writer during IO."""
    root = store.job(job_id)['root']
    same = bool(previous.get('store'))
    source_context = previous['store'].db() if same else closing(sqlite3.connect(previous['file'].resolve().as_uri()+'?mode=ro',uri=True))
    with source_context as source:
        available = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tasks = []
        inventory = 'entries' if same else 'inventory'
        where = ' WHERE job=?' if same else ''
        args = (previous['id'],) if same else ()
        if inventory in available:
            tasks.append(('baseline','SELECT path,kind,meta FROM '+inventory+where,args,0,None))
        if same:
            tasks.append(('baseline',"SELECT b.path,b.kind,b.meta FROM baseline b LEFT JOIN changes c ON c.job=b.job AND c.path=b.path WHERE b.job=? AND (c.status='unknown' OR ?=0)",(previous['id'],previous['job']['scan_complete']),0,None))
        for table,columns,remote,local in (
            ('names','identity,local,folded',None,1),
            ('artifacts','path,type,local,size,sha256,fingerprint,preview_size',0,2),
            ('partials','path,type,local,fingerprint,size,sha256,etag,total',0,2),
        ):
            if table in available:
                tasks.append((table,'SELECT '+columns+' FROM '+table+where,args,remote,local))
        # An external manifest is not trusted to skip hashing local content.
        if same:
            tasks.append(('verification_cache','SELECT local,signature FROM verification_cache'+where,args,None,0))
        count = 0
        for table,sql,parameters,remote,local in tasks:
            rows = previous['store'].rows(sql,parameters) if same else source.execute(sql,parameters)
            batch = []
            for record in rows:
                checkpoint()
                row = tuple(record.values()) if isinstance(record,dict) else tuple(record)
                if remote is not None: remote_path(row[remote])
                if local is not None: safe_join(root,row[local])
                batch.append((job_id,*row))
                if len(batch)>=200:
                    with store.db() as target:
                        target.executemany('INSERT OR IGNORE INTO '+table+' VALUES('+','.join('?' for _ in batch[0])+')',batch)
                    count+=len(batch); progress(count); batch.clear()
            if batch:
                with store.db() as target:
                    target.executemany('INSERT OR IGNORE INTO '+table+' VALUES('+','.join('?' for _ in batch[0])+')',batch)
                count+=len(batch); progress(count)


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
