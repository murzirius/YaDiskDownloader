import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .file_types import file_type, preview_type, selected_type


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, settings TEXT NOT NULL, root TEXT NOT NULL,
 service TEXT NOT NULL, phase TEXT NOT NULL, scan_complete INTEGER DEFAULT 0,
 started TEXT NOT NULL, ended TEXT, error TEXT DEFAULT '', downloaded INTEGER DEFAULT 0,
 zip_bytes INTEGER DEFAULT 0, zip_done INTEGER DEFAULT 0, zip_total INTEGER DEFAULT 0,
 zip_path TEXT, generation INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
 job TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, meta TEXT NOT NULL,
 local TEXT, scan_offset INTEGER DEFAULT 0, scanned INTEGER DEFAULT 0,
 status TEXT DEFAULT 'pending', error TEXT DEFAULT '',
 original_status TEXT DEFAULT 'not_requested', preview_status TEXT DEFAULT 'not_requested',
 fallback INTEGER DEFAULT 0, PRIMARY KEY(job,path)
);
CREATE INDEX IF NOT EXISTS queue ON entries(job,kind,status);
CREATE INDEX IF NOT EXISTS scan ON entries(job,kind,scanned);
CREATE TABLE IF NOT EXISTS names (
 job TEXT NOT NULL, identity TEXT NOT NULL, local TEXT NOT NULL, folded TEXT NOT NULL,
 PRIMARY KEY(job,identity), UNIQUE(job,folded)
);
CREATE TABLE IF NOT EXISTS artifacts (
 job TEXT NOT NULL, path TEXT NOT NULL, type TEXT NOT NULL, local TEXT NOT NULL,
 size INTEGER NOT NULL, sha256 TEXT NOT NULL, fingerprint TEXT NOT NULL,
 preview_size TEXT, PRIMARY KEY(job,path,type), UNIQUE(job,local)
);
CREATE TABLE IF NOT EXISTS scan_errors (
 job TEXT NOT NULL, path TEXT NOT NULL, error TEXT NOT NULL,
 PRIMARY KEY(job,path)
);
CREATE TABLE IF NOT EXISTS verification_cache (
 job TEXT NOT NULL, local TEXT NOT NULL, signature TEXT NOT NULL,
 PRIMARY KEY(job,local)
);
CREATE TABLE IF NOT EXISTS journal_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, time TEXT NOT NULL,
 category TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_job ON journal_events(job,id);
CREATE TABLE IF NOT EXISTS profiles (name TEXT PRIMARY KEY, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS partials (
 job TEXT NOT NULL, path TEXT NOT NULL, type TEXT NOT NULL, local TEXT NOT NULL,
 fingerprint TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL,
 etag TEXT NOT NULL DEFAULT '', total INTEGER,
 PRIMARY KEY(job,path,type), UNIQUE(job,local)
);
"""


class Store:
    def __init__(self, filename):
        self.filename = str(filename)
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.filename, timeout=30)) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        with self.db() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
            for name in ("original_attempts", "preview_attempts", "priority"):
                if name not in columns:
                    db.execute(f"ALTER TABLE entries ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS queue_priority ON entries(job,kind,status,priority,path)")
            if "pause_reason" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN pause_reason TEXT NOT NULL DEFAULT ''")
            db.execute("DELETE FROM verification_cache")
            for row in db.execute("SELECT id,settings FROM jobs").fetchall():
                settings = json.loads(row["settings"])
                if "attempts" not in settings:
                    settings["attempts"] = max(1, settings.get("retries", 3))
                    settings["retries"] = settings["attempts"] - 1
                    db.execute("UPDATE jobs SET settings=? WHERE id=?", (json.dumps(settings, ensure_ascii=False), row["id"]))
            db.execute("UPDATE jobs SET phase='interrupted', error='Процесс был перезапущен. Подключите Диск и продолжите.' WHERE phase IN ('scanning','downloading','packing','pausing','stopping')")
            db.execute("UPDATE entries SET status='pending' WHERE status='running'")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.filename, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def job(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Задание не найдено")
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        return result

    def update_job(self, job_id, **fields):
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), job_id))

    def update_entry(self, job_id, path, **fields):
        with self.db() as db:
            db.execute("UPDATE entries SET " + ",".join(f"{k}=?" for k in fields) + " WHERE job=? AND path=?", (*fields.values(), job_id, path))

    def insert_entry(self, db, job_id, meta, settings=None):
        if meta["type"] == "file" and settings is not None and not selected_type(meta, settings):
            return
        safe = {k: meta[k] for k in ("path", "name", "type", "size", "md5", "sha256", "modified", "resource_id", "mime_type", "media_type") if k in meta}
        safe["file_type"] = file_type(meta)
        safe["has_preview"] = preview_type(meta) and bool(meta.get("preview") or meta.get("has_preview"))
        db.execute("INSERT OR IGNORE INTO entries(job,path,kind,meta) VALUES(?,?,?,?)", (job_id, meta["path"], meta["type"], json.dumps(safe, ensure_ascii=False)))

    def rows(self, sql, params=(), batch=200):
        with self.db() as db:
            cursor = db.execute(sql, params)
            while rows := cursor.fetchmany(batch):
                yield from (dict(r) for r in rows)

    def summary(self, job_id):
        job = self.job(job_id)
        with self.db() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM entries WHERE job=? AND kind='file' GROUP BY status", (job_id,)).fetchall())
            found = db.execute("SELECT kind,COUNT(*) FROM entries WHERE job=? GROUP BY kind", (job_id,)).fetchall()
            totals = db.execute("SELECT a.type,COUNT(*),COALESCE(SUM(a.size),0) FROM artifacts a JOIN entries e ON e.job=a.job AND e.path=a.path WHERE a.job=? GROUP BY a.type", (job_id,)).fetchall()
            volume = db.execute("SELECT COALESCE(SUM(json_extract(meta,'$.size')),0),SUM(CASE WHEN json_extract(meta,'$.size') IS NULL THEN 1 ELSE 0 END),SUM(CASE WHEN json_extract(meta,'$.has_preview')=1 THEN 1 ELSE 0 END) FROM entries WHERE job=? AND kind='file'", (job_id,)).fetchone()
            scan_errors = db.execute("SELECT COUNT(*) FROM scan_errors WHERE job=?", (job_id,)).fetchone()[0]
            missing = db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND preview_status='unavailable'", (job_id,)).fetchone()[0]
            replaced = db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND fallback=1 AND status IN ('done','existing')", (job_id,)).fetchone()[0]
        found = dict(found)
        total = found.get("file", 0)
        processed = sum(counts.get(k, 0) for k in ("done", "existing", "partial", "error", "no_preview", "cancelled", "filtered"))
        originals = next((r[1] for r in totals if r[0] == "original"), 0)
        previews = next((r[1] for r in totals if r[0] == "preview"), 0)
        saved = sum(r[2] for r in totals)
        unknown = int(volume[1] or 0)
        preview_count = int(volume[2] or 0)
        mode = "fallback" if job["settings"].get("operation") == "missing" or job["generation"] > 0 else job["settings"]["mode"]
        with self.db() as db:
            original_condition = "(?!='previews' OR COALESCE(json_extract(e.meta,'$.file_type'),'photos') NOT IN ('photos','videos'))"
            originals_volume = db.execute("SELECT COALESCE(SUM(json_extract(e.meta,'$.size')),0),COALESCE(SUM(json_extract(e.meta,'$.size') IS NULL),0) FROM entries e WHERE e.job=? AND e.kind='file' AND " + original_condition, (job_id, mode)).fetchone()
            pending_original = original_condition + " AND e.original_status NOT IN ('failed','downloaded_verified','existing_verified')"
            pending = db.execute("SELECT COALESCE(SUM(CASE WHEN " + pending_original + " THEN COALESCE(json_extract(e.meta,'$.size'),0) ELSE 0 END),0),COALESCE(SUM(CASE WHEN " + pending_original + " THEN json_extract(e.meta,'$.size') IS NULL ELSE CASE WHEN COALESCE(json_extract(e.meta,'$.file_type'),'photos') IN ('photos','videos') AND e.original_status!='existing_verified' AND (?!='fallback' OR e.original_status='failed') THEN 1 ELSE 0 END END),0),COUNT(*),COALESCE(SUM(COALESCE(json_extract(e.meta,'$.file_type'),'photos') IN ('photos','videos')),0) FROM entries e WHERE e.job=? AND e.kind='file' AND e.status IN ('pending','running')", (mode, mode, mode, job_id)).fetchone()
        known = int(originals_volume[0])
        unknown_downloads = int(originals_volume[1])
        if mode in ("previews", "both"):
            unknown_downloads += preview_count
        job.update(counts=counts, files=total, folders=found.get("dir", 0), processed=processed,
                   percent=round(processed * 100 / total, 2) if job["scan_complete"] and total else (100 if job["scan_complete"] else None),
                   originals=originals, previews=previews, replacements=replaced, existing=counts.get("existing", 0),
                   missing_previews=missing, cancelled=counts.get("cancelled", 0), errors=counts.get("error", 0) + counts.get("partial", 0),
                   scan_errors=scan_errors, saved_bytes=saved,
                   remaining_work={"bytes": int(pending[0]), "unknown_files": int(pending[1]), "files": int(pending[2]), "preview_files": int(pending[3])},
                   estimate={"known_bytes": known, "unknown_sizes": unknown_downloads,
                             "original_bytes": int(volume[0]), "original_unknown": unknown,
                             "preview_candidates": preview_count,
                             "conditional_preview_sizes": preview_count if mode == "fallback" else 0,
                             "zip_basis_bytes": known if job["settings"].get("create_zip", True) else 0,
                             "extra_temporary_bytes": (2 if job["settings"].get("create_zip", True) else 1) * known, "is_estimate": True})
        return job
