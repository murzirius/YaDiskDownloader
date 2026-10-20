import json
import sqlite3
import tempfile
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from .file_types import file_type, preview_type, selected_type
from .exclusions import excluded
from .api import validate_resource
from .limits import MAX_WORKERS, normalize_saved_workers
from .sqlite_support import journal_mode
from .sqlite_writes import gate_for, WriteConnection
from .paths import remote_path
from .exclusions import validate as validate_exclusions
from .rate_limit import validate as validate_speed


SCHEMA = """
CREATE TABLE IF NOT EXISTS baseline (job TEXT,path TEXT,kind TEXT,meta TEXT,PRIMARY KEY(job,path));
CREATE TABLE IF NOT EXISTS changes (job TEXT,path TEXT,status TEXT,before_meta TEXT,after_meta TEXT,PRIMARY KEY(job,path));
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
 preview_size TEXT, PRIMARY KEY(job,path,type)
);
CREATE TABLE IF NOT EXISTS scan_errors (
 job TEXT NOT NULL, path TEXT NOT NULL, error TEXT NOT NULL,
 PRIMARY KEY(job,path)
);
CREATE TABLE IF NOT EXISTS verification_cache (
 job TEXT NOT NULL, local TEXT NOT NULL, signature TEXT NOT NULL,
 PRIMARY KEY(job,local)
);
CREATE TABLE IF NOT EXISTS verification_results (
 run TEXT,path TEXT,type TEXT,local TEXT,status TEXT,PRIMARY KEY(run,path,type)
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
CREATE TABLE IF NOT EXISTS partial_ownership (job TEXT,local TEXT,PRIMARY KEY(job,local));
CREATE TABLE IF NOT EXISTS service_outputs (root TEXT,kind TEXT,local TEXT,sha256 TEXT,PRIMARY KEY(root,kind));
CREATE TABLE IF NOT EXISTS service_output_signatures (root TEXT,kind TEXT,local TEXT,signature TEXT,PRIMARY KEY(root,kind));
CREATE TABLE IF NOT EXISTS temporary_files (root TEXT,local TEXT,purpose TEXT,created REAL,PRIMARY KEY(root,local));
CREATE TABLE IF NOT EXISTS duplicate_candidates (job TEXT,local TEXT,canonical TEXT,type TEXT,size INTEGER,sha256 TEXT,PRIMARY KEY(job,local));
CREATE TABLE IF NOT EXISTS duplicate_moves (job TEXT,local TEXT,quarantine TEXT,sha256 TEXT,size INTEGER,PRIMARY KEY(job,local));
CREATE TABLE IF NOT EXISTS pending_publications (job TEXT,path TEXT,type TEXT,temp TEXT,local TEXT,size INTEGER,sha256 TEXT,fingerprint TEXT,preview_size TEXT,PRIMARY KEY(job,path,type));
CREATE TABLE IF NOT EXISTS run_selection (run TEXT,path TEXT,PRIMARY KEY(run,path));
CREATE TABLE IF NOT EXISTS job_queue (job TEXT PRIMARY KEY,position INTEGER,action TEXT,state TEXT DEFAULT 'waiting',error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS report_records (root TEXT,path TEXT,kind TEXT,data TEXT,PRIMARY KEY(root,path,kind));
CREATE TABLE IF NOT EXISTS journal_jobs (job TEXT PRIMARY KEY,root TEXT);
CREATE INDEX IF NOT EXISTS jobs_root ON jobs(root,id);
CREATE INDEX IF NOT EXISTS journal_root ON journal_jobs(root,job);
CREATE TABLE IF NOT EXISTS pending_service_outputs (root TEXT,kind TEXT,local TEXT,sha256 TEXT,PRIMARY KEY(root,kind));
"""


class Store:
    def __init__(self, filename):
        mode = journal_mode()
        self.wal = mode == 'WAL'
        self.filename = str(filename)
        self.write_gate = gate_for(self.filename)
        self._sessions = threading.local()
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        from .backups import before_upgrade, recovery_source
        before_upgrade(Path(filename))
        connection = recovery_source(filename, timeout=30) if Path(filename).is_file() else sqlite3.connect(self.filename, timeout=30)
        with closing(connection) as setup:
            setup.execute("PRAGMA journal_mode=" + mode)
        with self.db() as db:
            db.executescript(SCHEMA)
            db.execute('DELETE FROM run_selection')
            # Older versions allowed just one remote reference per local file.
            # Migrate transactionally; the upgrade backup retains the old schema.
            unique_local = any(row[2] and [r[2] for r in db.execute('PRAGMA index_info("'+row[1]+'")')] == ['job','local'] for row in db.execute('PRAGMA index_list(artifacts)').fetchall())
            if unique_local:
                db.execute('ALTER TABLE artifacts RENAME TO artifacts_legacy')
                db.execute('CREATE TABLE artifacts (job TEXT NOT NULL,path TEXT NOT NULL,type TEXT NOT NULL,local TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,fingerprint TEXT NOT NULL,preview_size TEXT,PRIMARY KEY(job,path,type))')
                db.execute('INSERT INTO artifacts SELECT * FROM artifacts_legacy')
                db.execute('DROP TABLE artifacts_legacy')
            db.execute('CREATE INDEX IF NOT EXISTS artifact_content ON artifacts(job,type,sha256,size,preview_size)')
            db.execute('CREATE INDEX IF NOT EXISTS artifact_local ON artifacts(job,local)')
            columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
            for name in ("original_attempts", "preview_attempts", "priority"):
                if name not in columns:
                    db.execute(f"ALTER TABLE entries ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS queue_priority ON entries(job,kind,status,priority,path)")
            job_columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name, declaration in (("title", "TEXT NOT NULL DEFAULT ''"), ("archived", "INTEGER NOT NULL DEFAULT 0")):
                if name not in job_columns:
                    db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + declaration)
            if "pause_reason" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN pause_reason TEXT NOT NULL DEFAULT ''")
            for row in db.execute("SELECT id,settings FROM jobs").fetchall():
                try:
                    settings = json.loads(row["settings"])
                    if not isinstance(settings, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    db.execute("UPDATE jobs SET phase='failed',error=? WHERE id=?", ("Настройки задания повреждены. Создайте дозагрузку в прежнюю папку.", row["id"]))
                    continue
                changed = normalize_saved_workers(settings)
                if 'total_timeout' not in settings:
                    settings['total_timeout'] = settings.get('timeout', 20)
                    changed = True
                if "attempts" not in settings:
                    retries = settings.get("retries", 0)
                    settings["attempts"] = min(9, max(1, retries + 1)) if isinstance(retries, int) and not isinstance(retries, bool) else 1
                    settings["retries"] = settings["attempts"] - 1
                    changed = True
                if settings.pop("duplicate_cleanup", None) is not None:
                    changed = True
                if settings.get("adaptive_workers") is not False:
                    settings["adaptive_workers"] = False
                    changed = True
                if changed:
                    db.execute("UPDATE jobs SET settings=? WHERE id=?", (json.dumps(settings, ensure_ascii=False), row["id"]))
            for row in db.execute("SELECT name,settings FROM profiles").fetchall():
                try:
                    settings = json.loads(row["settings"])
                    if not isinstance(settings, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    continue
                workers_changed = normalize_saved_workers(settings)
                if settings.pop("duplicate_cleanup", None) is not None:
                    workers_changed = True
                if 'total_timeout' not in settings:
                    settings['total_timeout'] = settings.get('timeout', 20)
                    workers_changed = True
                if workers_changed or settings.get("adaptive_workers") is not False:
                    settings["adaptive_workers"] = False
                    db.execute("UPDATE profiles SET settings=? WHERE name=?", (json.dumps(settings, ensure_ascii=False), row["name"]))
            db.execute("UPDATE entries SET error='' WHERE status IN ('done','existing','partial') AND EXISTS(SELECT 1 FROM artifacts a WHERE a.job=entries.job AND a.path=entries.path) AND (error LIKE 'Оригинал:%' OR error LIKE 'Превью:%') AND error NOT LIKE '%места%' AND error NOT LIKE '%файловой системы%' AND error NOT LIKE '%локальный путь%' AND error NOT LIKE '%пределы папки%'")
            db.execute("UPDATE jobs SET phase='interrupted', error='Процесс был перезапущен. Подключите Диск и продолжите.' WHERE phase IN ('new','scanning','preparing','downloading','packing','pausing','stopping')")
            db.execute("UPDATE entries SET status='pending' WHERE status='running'")
            db.execute("UPDATE job_queue SET state='waiting',error='' WHERE state='running'")
            db.execute("UPDATE jobs SET phase='completed' WHERE phase='completed_with_errors' AND error='' AND NOT EXISTS(SELECT 1 FROM scan_errors s WHERE s.job=jobs.id) AND NOT EXISTS(SELECT 1 FROM entries e WHERE e.job=jobs.id AND e.kind='file' AND (e.error!='' OR e.status IN ('pending','running','error','no_preview','cancelled')))")

    @contextmanager
    def activity(self):
        """Keep WAL open during a job without holding a read/write transaction.

        Individual db() calls still commit with synchronous=FULL. Keeping one
        idle connection avoids checkpointing/reopening WAL after every short
        transaction when the last worker connection happens to close.
        """
        with self.db() as connection:
            connection.execute('SELECT COUNT(*) FROM sqlite_master').fetchall()
            yield

    @contextmanager
    def session(self):
        """Reuse a connection inside one file, preserving every commit boundary.

        Nested db() scopes still receive separate connections. No connection is
        shared between threads or retained after the file operation finishes.
        """
        current = getattr(self._sessions, 'scope', None)
        if current is not None:
            yield
            return
        scope = {'connection':None, 'borrowed':False}
        self._sessions.scope = scope
        try:
            yield
        finally:
            self._sessions.scope = None
            if scope['connection'] is not None:
                scope['connection'].close()

    def _connect(self):
        conn = sqlite3.connect(self.filename, timeout=30, factory=WriteConnection)
        conn.gate = self.write_gate
        try:
            conn.create_function('unicode_fold', 1, lambda value: (value or '').casefold(), deterministic=True)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def db(self):
        scope = getattr(self._sessions, 'scope', None)
        reuse = scope is not None and not scope['borrowed']
        if reuse:
            if scope['connection'] is None:
                scope['connection'] = self._connect()
            conn = scope['connection']
            scope['borrowed'] = True
        else:
            conn = self._connect()
        discard = committing = False
        try:
            yield conn
            committing = True
            conn.commit()
            committing = False
        except BaseException as exc:
            discard = committing or isinstance(exc, sqlite3.Error)
            try:
                conn.rollback()
            except BaseException:
                discard = True
                raise
            raise
        finally:
            if reuse:
                try:
                    if discard:
                        scope['connection'] = None
                        conn.close()
                    else:
                        conn.release_admission()
                finally:
                    scope['borrowed'] = False
            else:
                conn.close()

    def job(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Задание не найдено")
        result = dict(row)
        try:
            if not isinstance(result['root'], str) or not Path(result['root']).is_absolute():
                result['root'] = ''
                raise ValueError()
            result["settings"] = json.loads(result["settings"])
            if not isinstance(result["settings"], dict):
                raise ValueError()
            normalize_saved_workers(result['settings'])
            defaults = {"mode":"fallback", "preview_size":"XXXL", "reports":["json"],
                        "file_types":["photos","videos","music","files"], "paths":[],
                        "whole_disk":False, "destination":result["root"], "workers":8,
                        "attempts":1, "retries":0, "timeout":20, "total_timeout":20,
                        "missing_policy":"originals", "operation":"new", "exclusions":[]}
            for key, value in defaults.items():
                result['settings'].setdefault(key, value)
            settings = result['settings']
            if settings['mode'] not in ('previews','both','fallback') or settings['preview_size'] not in ('S','M','L','XL','XXL','XXXL'):
                raise ValueError()
            if any(not isinstance(settings[key], list) for key in ('paths','reports','file_types')):
                raise ValueError()
            if not settings['paths'] or any(not isinstance(path, str) or not path.startswith('disk:/') for path in settings['paths']):
                raise ValueError()
            if not settings['reports'] or any(value not in ('json','csv','html','txt') for value in settings['reports']):
                raise ValueError()
            if (not isinstance(settings['exclusions'], list)
                    or any(not isinstance(value, str) for value in settings['exclusions'])
                    or any(value not in ('photos','videos','music','files') for value in settings['file_types'])
                    or not settings['file_types']
                    or settings['operation'] not in ('new','missing')
                    or settings['missing_policy'] not in ('originals','any_result','both')
                    or not isinstance(settings['whole_disk'], bool)):
                raise ValueError()
            if any(isinstance(settings[key], bool) or not isinstance(settings[key], int) or not low <= settings[key] <= high for key,low,high in (('attempts',1,9),('timeout',5,3600),('total_timeout',0,86400))):
                raise ValueError()
            workers = settings['workers']
            if isinstance(workers,bool) or not isinstance(workers,int) or not 1 <= workers <= MAX_WORKERS:
                raise ValueError()
            for path in settings['paths']:
                remote_path(path)
            validate_exclusions(settings['exclusions'])
            validate_speed(settings.get('speed_limit_mib',0))
            for key in ('create_zip','auto_pause','resume_parts','adaptive_workers'):
                if key in settings and not isinstance(settings[key],bool):
                    raise ValueError()
            fullness = settings.get('max_disk_percent',95)
            if isinstance(fullness,bool) or not isinstance(fullness,(int,float)) or not 1 <= fullness <= 99.9:
                raise ValueError()
            if '_inherit_source' in settings:
                source = settings['_inherit_source']
                if not isinstance(source,dict) or set(source) not in ({'job'},{'manifest'}) or not isinstance(next(iter(source.values())),str) or not next(iter(source.values())):
                    raise ValueError()
        except (ValueError, TypeError):
            result["settings"] = {"_invalid": True, "mode": "fallback", "preview_size": "XXXL", "reports": ["json"], "file_types": ["photos", "videos", "music", "files"], "paths": [], "whole_disk": False, "destination": result["root"], "workers": 8, "attempts": 1, "retries": 0, "timeout": 20, "total_timeout": 20}
            result['error'] = "Настройки этого задания повреждены или неполны. Скачанные файлы сохранены. Выберите «Дозагрузить недостающее» и укажите прежнюю папку; создавать другие задания можно."
        return result

    def update_job(self, job_id, **fields):
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), job_id))

    def update_entry(self, job_id, path, **fields):
        with self.db() as db:
            db.execute("UPDATE entries SET " + ",".join(f"{k}=?" for k in fields) + " WHERE job=? AND path=?", (*fields.values(), job_id, path))

    def insert_entry(self, db, job_id, meta, settings=None):
        validate_resource(meta)
        if settings is not None and excluded(meta["path"], settings):
            return
        if meta["type"] == "file" and settings is not None and not selected_type(meta, settings):
            return
        safe = {k: meta[k] for k in ("path", "name", "type", "size", "md5", "sha256", "modified", "resource_id", "mime_type", "media_type") if k in meta}
        safe["file_type"] = file_type(meta)
        safe["has_preview"] = preview_type(meta) and bool(meta.get("preview") or meta.get("has_preview"))
        db.execute("INSERT OR IGNORE INTO entries(job,path,kind,meta) VALUES(?,?,?,?)", (job_id, meta["path"], meta["type"], json.dumps(safe, ensure_ascii=False)))

    def rows(self, sql, params=(), batch=200):
        if not self.wal:
            # Rollback-journal readers block a writer's commit. Some callers
            # write while consuming this iterator (reports, cache repair).
            # Finish the read first; spill large snapshots to an owned temporary
            # file rather than keeping an unbounded list in memory.
            with tempfile.SpooledTemporaryFile(max_size=1024*1024, mode='w+t', encoding='utf-8', dir=Path(self.filename).parent) as snapshot:
                with self.db() as db:
                    cursor = db.execute(sql, params)
                    while rows := cursor.fetchmany(batch):
                        for row in rows:
                            snapshot.write(json.dumps(dict(row), ensure_ascii=False)+'\n')
                snapshot.seek(0)
                for line in snapshot:
                    yield json.loads(line)
            return
        with self.db() as db:
            cursor = db.execute(sql, params)
            while rows := cursor.fetchmany(batch):
                yield from (dict(r) for r in rows)

    def summary(self, job_id):
        job = self.job(job_id)
        mode = ("both" if job['settings'].get('missing_policy') == 'both' else "fallback") if job["settings"].get("operation") == "missing" or job["generation"] > 0 else job["settings"]["mode"]
        if job['settings'].get('missing_policy') == 'both':mode='both'
        with self.db() as db:
            # One indexed pass instead of independent scans for each counter.
            # Flattened read-only subqueries avoid inventory copies in RAM and
            # never enter the writer gate (WITH is conservatively a writer).
            grouped = db.execute("""SELECT kind,status,COUNT(*) AS total,COALESCE(SUM(size),0) AS bytes,
                SUM(size IS NULL) AS unknown_sizes,
                SUM(CASE WHEN has_preview=1 THEN 1 ELSE 0 END) AS preview_candidates,
                SUM(CASE WHEN preview_status='unavailable' THEN 1 ELSE 0 END) AS missing,
                SUM(CASE WHEN fallback=1 AND status IN ('done','existing') THEN 1 ELSE 0 END) AS replaced,
                SUM(CASE WHEN status NOT IN ('pending','running','cancelled') AND
                    (status IN ('error','no_preview') OR error!='') THEN 1 ELSE 0 END) AS failures,
                COALESCE(SUM(CASE WHEN :mode!='previews' OR media_type NOT IN ('photos','videos') THEN COALESCE(size,0) ELSE 0 END),0) AS original_bytes,
                SUM(CASE WHEN (:mode!='previews' OR media_type NOT IN ('photos','videos')) AND size IS NULL THEN 1 ELSE 0 END) AS original_unknown,
                COALESCE(SUM(CASE WHEN status IN ('pending','running') AND
                    (:mode!='previews' OR media_type NOT IN ('photos','videos')) AND
                    original_status NOT IN ('failed','downloaded_verified','existing_verified')
                    THEN COALESCE(size,0) ELSE 0 END),0) AS pending_bytes,
                SUM(CASE WHEN status IS NULL OR status NOT IN ('pending','running') THEN 0
                    WHEN (:mode!='previews' OR media_type NOT IN ('photos','videos')) AND
                        original_status NOT IN ('failed','downloaded_verified','existing_verified') THEN size IS NULL
                    WHEN media_type IN ('photos','videos') AND original_status!='existing_verified' AND
                        (:mode!='fallback' OR original_status='failed') THEN 1 ELSE 0 END) AS pending_unknown,
                SUM(CASE WHEN status IN ('pending','running') AND media_type IN ('photos','videos') THEN 1 ELSE 0 END) AS pending_previews
                FROM (
                    SELECT *,json_extract(file_meta,'$.size') AS size,
                        COALESCE(json_extract(file_meta,'$.file_type'),'photos') AS media_type,
                        json_extract(file_meta,'$.has_preview') AS has_preview FROM (
                        SELECT kind,status,original_status,preview_status,fallback,error,
                            CASE WHEN kind='file' THEN meta ELSE '{}' END AS file_meta
                        FROM entries WHERE job=:job
                    ) AS inputs
                ) AS values_ GROUP BY kind,status""", {'job':job_id,'mode':mode}).fetchall()
            counts, found = {}, {}
            volume, originals_volume, pending = [0,0,0], [0,0], [0,0,0,0]
            missing = replaced = failures = 0
            for row in grouped:
                found[row['kind']] = found.get(row['kind'],0)+row['total']
                missing += row['missing']; replaced += row['replaced']
                if row['kind'] != 'file':
                    continue
                counts[row['status']] = row['total']
                failures += row['failures']
                for index, key in enumerate(('bytes','unknown_sizes','preview_candidates')):
                    volume[index] += row[key]
                for index, key in enumerate(('original_bytes','original_unknown')):
                    originals_volume[index] += row[key]
                for index, key in enumerate(('pending_bytes','pending_unknown')):
                    pending[index] += row[key]
                if row['status'] in ('pending','running'):
                    pending[2] += row['total']
                pending[3] += row['pending_previews']
            totals = db.execute("SELECT a.type,COUNT(*),COALESCE(SUM(a.size),0) FROM artifacts a JOIN entries e ON e.job=a.job AND e.path=a.path WHERE a.job=? GROUP BY a.type", (job_id,)).fetchall()
            scan_errors = db.execute("SELECT COUNT(*) FROM scan_errors WHERE job=?", (job_id,)).fetchone()[0]
            previous_results = db.execute("SELECT COUNT(DISTINCT e.path) FROM entries e JOIN artifacts a ON a.job=e.job AND a.path=e.path WHERE e.job=? AND e.kind='file' AND e.priority>0", (job_id,)).fetchone()[0]
            completeness = db.execute("""SELECT COUNT(*),COALESCE(SUM(has_original),0),
                COALESCE(SUM(NOT has_original AND has_preview),0),
                COALESCE(SUM(NOT has_original AND NOT has_preview),0) FROM (
                SELECT EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='original') has_original,
                       EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='preview') has_preview
                FROM entries e WHERE e.job=? AND e.kind='file' AND e.status!='filtered')""", (job_id,)).fetchone()
        total = found.get("file", 0)
        processed = sum(counts.get(k, 0) for k in ("done", "existing", "partial", "error", "no_preview", "filtered"))
        originals = next((r[1] for r in totals if r[0] == "original"), 0)
        previews = next((r[1] for r in totals if r[0] == "preview"), 0)
        with self.db() as db:
            physical = db.execute('SELECT COALESCE(SUM(size),0),COUNT(*) FROM (SELECT a.local,MAX(a.size) size FROM artifacts a JOIN entries e ON e.job=a.job AND e.path=a.path WHERE a.job=? GROUP BY a.local)',(job_id,)).fetchone()
            saved = physical[0]
            duplicates = sum(r[1] for r in totals)-physical[1]
        unknown = int(volume[1] or 0)
        preview_count = int(volume[2] or 0)
        missing_operation = job["settings"].get("operation") == "missing"
        known = int(pending[0]) if missing_operation else int(originals_volume[0])
        unknown_downloads = int(pending[1]) if missing_operation else int(originals_volume[1])
        if mode in ("previews", "both"):
            unknown_downloads += preview_count
        job.update(counts=counts, files=total, folders=found.get("dir", 0), processed=processed,
                   percent=round(processed * 100 / total, 2) if job["scan_complete"] and total else (100 if job["scan_complete"] else None),
                   originals=originals, previews=previews, replacements=replaced, existing=counts.get("existing", 0),
                   missing_previews=missing, cancelled=counts.get("cancelled", 0), errors=failures,
                   scan_errors=scan_errors, saved_bytes=saved, duplicates=duplicates, previous_results=previous_results,
                   completeness=dict(zip(('selected', 'originals', 'preview_only', 'no_result'), map(int, completeness))),
                   remaining_work={"bytes": int(pending[0]), "unknown_files": int(pending[1]), "files": int(pending[2]), "preview_files": int(pending[3])},
                   estimate={"known_bytes": known, "unknown_sizes": unknown_downloads,
                             "original_bytes": int(volume[0]), "original_unknown": unknown,
                             "preview_candidates": preview_count,
                             "conditional_preview_sizes": (int(pending[3]) if missing_operation else preview_count) if mode == "fallback" else 0,
                             "zip_basis_bytes": known if job["settings"].get("create_zip", True) else 0,
                             "extra_temporary_bytes": (2 if job["settings"].get("create_zip", True) else 1) * known, "is_estimate": True})
        return job
