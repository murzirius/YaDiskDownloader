import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from .paths import remote_path

from .paths import publish, replace_owned, reserve_output, safe_join, temporary_file


FORMAT = "disk-local-import-v1"


def find_import(store, root):
    normalized = os.path.normcase(str(Path(root).resolve()))
    for row in store.rows("SELECT id,root FROM jobs ORDER BY started DESC"):
        same_name = Path(root).name == "Disk-" + row["id"][:12]
        if os.path.normcase(str(Path(row["root"]).resolve())) == normalized or same_name:
            job = store.job(row["id"])
            return {"job": job, "store": store, "id": job["id"]}
    for filename in sorted(Path(root).glob(".disk-import*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            safe_join(root, filename.name)
            with closing(sqlite3.connect(filename.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                meta = dict(db.execute("SELECT key,value FROM import_meta"))
                if meta.get("format") == FORMAT:
                    settings = json.loads(meta["settings"])
                    if not isinstance(settings, dict) or not isinstance(settings.get("paths"), list):
                        continue
                    return {"job": {"settings": settings, "service": meta["service"], "generation": int(meta["generation"])}, "file": filename, "inventory_available": bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='inventory'").fetchone())}
        except (sqlite3.Error, OSError, ValueError, KeyError):
            continue
    return None


def inherit(store, job_id, previous):
    root = store.job(job_id)["root"]
    with store.db() as target:
        if previous.get("store"):
            with previous["store"].db() as source:
                for row in source.execute("SELECT path,kind,meta FROM entries WHERE job=?", (previous["id"],)):
                    remote_path(row[0])
                    target.execute("INSERT OR IGNORE INTO baseline VALUES(?,?,?,?)", (job_id, *row))
                for row in source.execute("SELECT b.path,b.kind,b.meta FROM baseline b LEFT JOIN changes c ON c.job=b.job AND c.path=b.path WHERE b.job=? AND (c.status='unknown' OR ?=0)", (previous["id"], previous["job"]["scan_complete"])):
                    remote_path(row[0])
                    target.execute("INSERT OR IGNORE INTO baseline VALUES(?,?,?,?)", (job_id, *row))
                for row in source.execute("SELECT identity,local,folded FROM names WHERE job=?", (previous["id"],)):
                    safe_join(root, row[1])
                    target.execute("INSERT OR IGNORE INTO names VALUES(?,?,?,?)", (job_id, *row))
                for row in source.execute("SELECT local,signature FROM verification_cache WHERE job=?", (previous["id"],)):
                    safe_join(root, row[0])
                    target.execute("INSERT OR IGNORE INTO verification_cache VALUES(?,?,?)", (job_id, *row))
                for row in source.execute("SELECT path,type,local,size,sha256,fingerprint,preview_size FROM artifacts WHERE job=?", (previous["id"],)):
                    remote_path(row[0])
                    safe_join(root, row[2])
                    target.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job_id, *row))
                for row in source.execute("SELECT path,type,local,fingerprint,size,sha256,etag,total FROM partials WHERE job=?", (previous["id"],)):
                    remote_path(row[0])
                    safe_join(root, row[2])
                    target.execute("INSERT OR IGNORE INTO partials VALUES(?,?,?,?,?,?,?,?,?)", (job_id, *row))
        else:
            with closing(sqlite3.connect(previous["file"].resolve().as_uri() + "?mode=ro", uri=True)) as source:
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='verification_cache'").fetchone():
                    for row in source.execute("SELECT local,signature FROM verification_cache"):
                        safe_join(root, row[0])
                        target.execute("INSERT OR IGNORE INTO verification_cache VALUES(?,?,?)", (job_id, *row))
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='inventory'").fetchone():
                    for row in source.execute("SELECT path,kind,meta FROM inventory"):
                        remote_path(row[0])
                        target.execute("INSERT OR IGNORE INTO baseline VALUES(?,?,?,?)", (job_id, *row))
                for row in source.execute("SELECT identity,local,folded FROM names"):
                    safe_join(root, row[1])
                    target.execute("INSERT OR IGNORE INTO names VALUES(?,?,?,?)", (job_id, *row))
                for row in source.execute("SELECT path,type,local,size,sha256,fingerprint,preview_size FROM artifacts"):
                    remote_path(row[0])
                    safe_join(root, row[2])
                    target.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job_id, *row))
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='partials'").fetchone():
                    for row in source.execute("SELECT path,type,local,fingerprint,size,sha256,etag,total FROM partials"):
                        remote_path(row[0])
                        safe_join(root, row[2])
                        target.execute("INSERT OR IGNORE INTO partials VALUES(?,?,?,?,?,?,?,?,?)", (job_id, *row))


def save_import(store, job_id):
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
    temp = temporary_file(job["root"])
    try:
        with closing(sqlite3.connect(temp)) as db:
            db.executescript("CREATE TABLE import_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE names(identity TEXT PRIMARY KEY,local TEXT,folded TEXT); CREATE TABLE artifacts(path TEXT,type TEXT,local TEXT,size INTEGER,sha256 TEXT,fingerprint TEXT,preview_size TEXT,PRIMARY KEY(path,type)); CREATE TABLE partials(path TEXT,type TEXT,local TEXT,fingerprint TEXT,size INTEGER,sha256 TEXT,etag TEXT,total INTEGER,PRIMARY KEY(path,type)); CREATE TABLE inventory(path TEXT PRIMARY KEY,kind TEXT,meta TEXT);")
            db.execute("CREATE TABLE verification_cache(local TEXT PRIMARY KEY,signature TEXT NOT NULL)")
            db.executemany("INSERT INTO import_meta VALUES(?,?)", [("format", FORMAT), ("settings", json.dumps(job["settings"], ensure_ascii=False)), ("service", job["service"]), ("generation", str(job["generation"]))])
            with store.db() as source:
                db.executemany("INSERT INTO verification_cache VALUES(?,?)", source.execute("SELECT local,signature FROM verification_cache WHERE job=?", (job_id,)))
                db.executemany("INSERT INTO inventory VALUES(?,?,?)", source.execute("SELECT path,kind,meta FROM entries WHERE job=?", (job_id,)))
                db.executemany("INSERT OR IGNORE INTO inventory VALUES(?,?,?)", source.execute("SELECT b.path,b.kind,b.meta FROM baseline b LEFT JOIN changes c ON c.job=b.job AND c.path=b.path WHERE b.job=? AND (c.status='unknown' OR ?=0)", (job_id, job["scan_complete"])))
                db.executemany("INSERT INTO names VALUES(?,?,?)", source.execute("SELECT identity,local,folded FROM names WHERE job=?", (job_id,)))
                db.executemany("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", source.execute("SELECT path,type,local,size,sha256,fingerprint,preview_size FROM artifacts WHERE job=?", (job_id,)))
                db.executemany("INSERT INTO partials VALUES(?,?,?,?,?,?,?,?)", source.execute("SELECT path,type,local,fingerprint,size,sha256,etag,total FROM partials WHERE job=?", (job_id,)))
            db.commit()
        target = safe_join(job["root"], local)
        if owned and os.path.exists(target):
            replace_owned(temp, target)
        else:
            publish(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
