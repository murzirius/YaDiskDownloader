import json
import os
from pathlib import Path

from .exclusions import excluded
from .file_types import selected_type
from .journal import encoded
from .paths import forget_temporary, temporary_file


LABELS = {"new": "Новые", "changed": "Изменённые", "unchanged": "Без изменений", "removed": "Больше нет в источнике", "excluded": "Исключены из выбора", "unknown": "Не удалось проверить"}


def stamp(meta):
    return {key: meta.get(key) for key in ("size", "md5", "sha256", "modified", "resource_id", "type")}


def compare(store, job_id, checkpoint=lambda: None):
    job = store.job(job_id)
    settings = job["settings"]
    with store.db() as db:
        errors = [r[0] for r in db.execute("SELECT path FROM scan_errors WHERE job=?", (job_id,))]
        db.execute("DELETE FROM changes WHERE job=?", (job_id,))
    selections = (
        ("SELECT e.path,e.meta,b.meta AS old FROM entries e LEFT JOIN baseline b ON b.job=e.job AND b.path=e.path WHERE e.job=? AND e.kind='file'", False),
        ("SELECT b.path,b.meta FROM baseline b WHERE b.job=? AND b.kind='file' AND NOT EXISTS(SELECT 1 FROM entries e WHERE e.job=b.job AND e.path=b.path)", True)
    )
    for sql, missing in selections:
        batch = []
        for row in store.rows(sql, (job_id,)):
            checkpoint()
            if not missing:
                status = "new" if row["old"] is None else "unchanged" if stamp(json.loads(row["old"])) == stamp(json.loads(row["meta"])) else "changed"
                batch.append((job_id, row["path"], status, row["old"], row["meta"]))
            else:
                path = row["path"]
                in_scope = any(root == "disk:/" or root == path or path.startswith(root + "/") for root in settings["paths"])
                if not in_scope or excluded(path, settings) or not selected_type(json.loads(row["meta"]), settings):
                    status = "excluded"
                elif not job["scan_complete"] or any(path == root or path.startswith(root.rstrip("/") + "/") for root in errors):
                    status = "unknown"
                else:
                    status = "removed"
                batch.append((job_id, path, status, row["meta"], None))
            if len(batch) == 200:
                with store.db() as db:
                    db.executemany("INSERT INTO changes VALUES(?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            with store.db() as db:
                db.executemany("INSERT INTO changes VALUES(?,?,?,?,?)", batch)


def change_summary(store, job_id):
    job = store.job(job_id)
    with store.db() as db:
        counts = dict(db.execute("SELECT status,COUNT(*) FROM changes WHERE job=? GROUP BY status", (job_id,)))
        baseline = bool(db.execute("SELECT 1 FROM baseline WHERE job=? LIMIT 1", (job_id,)).fetchone())
    return {"counts": {key: counts.get(key, 0) for key in LABELS}, "labels": LABELS, "compared": baseline or job["settings"].get("comparison_available", False), "complete": bool(job["scan_complete"])}


def folders(store, job_id, path="disk:/", offset=0):
    store.job(job_id)
    prefix = path.rstrip("/") + "/"
    with store.db() as db:
        rows = db.execute("""WITH files AS (
          SELECT path,substr(path,?) AS relative,json_extract(meta,'$.size') AS bytes,
          json_extract(meta,'$.has_preview') AS preview FROM entries
          WHERE job=? AND kind='file' AND substr(path,1,?)=?
        ), grouped AS (
          SELECT CASE WHEN instr(relative,'/')=0 THEN ? ELSE ? || substr(relative,1,instr(relative,'/')-1) END AS path,
          instr(relative,'/')=0 AS direct,COUNT(*) AS files,COALESCE(SUM(bytes),0) AS known_bytes,
          SUM(bytes IS NULL) AS unknown_sizes,COALESCE(SUM(preview=1),0) AS preview_files
          FROM files GROUP BY 1,2
        ) SELECT * FROM grouped ORDER BY known_bytes DESC,path LIMIT 101 OFFSET ?""", (len(prefix) + 1, job_id, len(prefix), prefix, path, prefix, offset)).fetchall()
    return {"path": path, "items": [dict(row) for row in rows[:100]], "has_more": len(rows) > 100, "next_offset": offset + min(100, len(rows))}


def write_changes(store, job_id, checkpoint=lambda: None):
    job = store.job(job_id)
    compare(store, job_id, checkpoint)
    from .service_outputs import location, finish
    relative, replacing = location(store, job, 'changes', 'changes.html', checkpoint)
    temp = temporary_file(job["root"], store, 'changes')
    template = Path(__file__).with_name("templates").joinpath("changes.html").read_text(encoding="utf-8")
    style = Path(__file__).with_name("templates").joinpath("journal.html").read_text(encoding="utf-8").split("<style>", 1)[1].split("</style>", 1)[0]
    template = template.replace('<html lang="ru">', '<html lang="ru" data-changes="' + job_id + '">', 1)
    before, after = template.replace("__STYLE__", style).split("__DATA__")
    try:
        with open(temp, "w", encoding="utf-8") as stream:
            stream.write(before)
            stream.write('{"summary":' + encoded(change_summary(store, job_id)) + ',"items":[')
            first = True
            for row in store.rows("SELECT path,status,before_meta,after_meta FROM changes WHERE job=? ORDER BY path", (job_id,)):
                checkpoint()
                item = {"path": row["path"], "status": row["status"], "before_size": json.loads(row["before_meta"]).get("size") if row["before_meta"] else None, "after_size": json.loads(row["after_meta"]).get("size") if row["after_meta"] else None}
                stream.write(("" if first else ",") + encoded(item))
                first = False
            stream.write("]}" + after)
            stream.flush()
            os.fsync(stream.fileno())
        finish(store, job, 'changes', relative, temp, replacing, checkpoint)
        current = store.job(job_id)["settings"]
        current["changes_local"] = relative
        store.update_job(job_id, settings=json.dumps(current, ensure_ascii=False))
        return relative
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
            forget_temporary(store,job['root'],temp)
