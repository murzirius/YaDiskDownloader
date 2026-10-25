import csv
import html
import json
import os
import threading
from itertools import groupby

from .paths import forget_temporary, safe_join, temporary_file
from .service_outputs import location, finish, root_key

REPORT_LOCK = threading.RLock()


def records(store, job_id):
    columns = ("type", "local", "size", "sha256", "fingerprint", "preview_size")
    selection = ",".join("a." + key + " AS result_" + key for key in columns)
    rows = store.rows("SELECT e.*," + selection + " FROM entries e LEFT JOIN artifacts a ON a.job=e.job AND a.path=e.path WHERE e.job=? ORDER BY e.path,a.type", (job_id,))
    for path, group in groupby(rows, key=lambda row: row["path"]):
        results = []
        entry = None
        for row in group:
            entry = row
            if row["result_type"] is not None:
                results.append({key: row["result_" + key] for key in columns})
        yield {"record_type": entry["kind"], "source_path": entry["path"], "local_path": entry["local"],
               "status": entry["status"] if entry["kind"] == "file" else "directory",
               "error": entry["error"], "original_attempt": entry["original_status"],
               "preview_attempt": entry["preview_status"], "fallback_to_preview": bool(entry["fallback"]),
               "results": results, "original_attempts": entry["original_attempts"], "preview_attempts": entry["preview_attempts"]}
    for row in store.rows("SELECT path,error FROM scan_errors WHERE job=? ORDER BY path", (job_id,)):
        yield {"record_type": "scan_error", "source_path": row["path"], "local_path": None, "status": "error",
               "error": row["error"], "original_attempt": "not_requested", "preview_attempt": "not_requested",
               "fallback_to_preview": False, "results": [], "original_attempts": 0, "preview_attempts": 0}


def context(store, job_id):
    job = store.summary(job_id)
    keys = ("id", "settings", "root", "started", "ended", "phase", "scan_complete", "files", "folders", "processed", "percent", "originals", "previews", "replacements", "existing", "missing_previews", "errors", "scan_errors", "cancelled", "downloaded", "saved_bytes", "error")
    return {k: job[k] for k in (*keys, 'completeness')}


def excel_cell(value):
    value = str(value) if value is not None else ""
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


def write_reports(store, job_id, checkpoint=lambda: None, final_phase=None):
    with REPORT_LOCK:
        return _write_reports(store, job_id, checkpoint, final_phase)


def merged_records(store, job, key=None):
    key = root_key(job['root']) if key is None else key
    for row in store.rows('SELECT data FROM report_records WHERE root=? ORDER BY path,kind', (key,)):
        yield json.loads(row['data'])


def _write_reports(store, job_id, checkpoint, final_phase):
    job = store.job(job_id)
    # Grouping key only: publication still checks paths and ownership afresh.
    key = root_key(job['root'])
    # Errors are current observations, not permanent history. Keep older errors
    # only while a source remains outside this job's successfully scanned scope.
    with store.db() as db:
        if job['scan_complete']:
            db.execute("""DELETE FROM report_records WHERE root=? AND kind='scan_error'
              AND EXISTS(SELECT 1 FROM json_each(?) scope WHERE scope.value='disk:/' OR scope.value=report_records.path
                OR substr(report_records.path,1,length(scope.value)+1)=scope.value||'/')
              AND NOT EXISTS(SELECT 1 FROM scan_errors s WHERE s.job=? AND (s.path=report_records.path
                OR substr(report_records.path,1,length(rtrim(s.path,'/'))+1)=rtrim(s.path,'/')||'/'))""", (key,json.dumps(job['settings']['paths']),job_id))
    batch = []
    for record in records(store, job_id):
        checkpoint()
        batch.append((key, record['source_path'], record['record_type'], json.dumps(record, ensure_ascii=False)))
        if len(batch) >= 200:
            with store.db() as db:
                db.executemany('INSERT OR REPLACE INTO report_records VALUES(?,?,?,?)', batch)
            batch.clear()
    if batch:
        with store.db() as db:
            db.executemany('INSERT OR REPLACE INTO report_records VALUES(?,?,?,?)', batch)
    summary = context(store, job_id)
    if final_phase:
        summary["phase"] = final_phase
    fields = ["record_type", "source_path", "local_path", "status", "error", "original_attempt", "preview_attempt", "fallback_to_preview", "results", "original_attempts", "preview_attempts"]
    paths = []
    for fmt in job["settings"]["reports"]:
        relative, replace = location(store, job, 'report:' + fmt, 'report.' + fmt, checkpoint)
        safe_join(job["root"], relative)
        temp = temporary_file(job["root"], store, 'report')
        try:
            with open(temp, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as out:
                if fmt == "json":
                    out.write('{"summary":' + json.dumps(summary, ensure_ascii=False) + ',"entries":[')
                    first = True
                    for row in merged_records(store, job, key):
                        checkpoint()
                        if not first:
                            out.write(",")
                        out.write(json.dumps(row, ensure_ascii=False))
                        first = False
                    out.write("]}")
                elif fmt == "csv":
                    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_ALL)
                    writer.writerow(["summary", *fields])
                    writer.writerow([excel_cell(json.dumps(summary, ensure_ascii=False)), "summary", *("" for _ in fields[1:])])
                    for row in merged_records(store, job, key):
                        checkpoint()
                        writer.writerow(["", *[excel_cell(json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (list, dict)) else row[k]) for k in fields]])
                elif fmt == "txt":
                    out.write("Локальная копия Яндекс.Диска — отчёт\n\nНастройки и итог:\n")
                    out.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n\nРесурсы:\n")
                    for row in merged_records(store, job, key):
                        checkpoint()
                        out.write(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
                elif fmt == "html":
                    out.write('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Отчёт Яндекс.Диск</title><style>body{font:14px system-ui;margin:clamp(12px,3vw,24px);background:#17191d;color:#e5e7eb}h1{font-size:clamp(20px,4vw,28px);overflow-wrap:anywhere}table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse;width:100%}th,td{border:1px solid #41454d;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere}th{background:#272b33}</style><h1>Отчёт Яндекс.Диск</h1><p>100% обработки может включать ошибки. Превью не является оригиналом.</p><pre>')
                    out.write(html.escape(json.dumps(summary, ensure_ascii=False, indent=2)))
                    out.write("</pre><table><thead><tr>" + "".join("<th>" + html.escape(k) + "</th>" for k in fields) + "</tr></thead><tbody>")
                    for row in merged_records(store, job, key):
                        checkpoint()
                        out.write("<tr>" + "".join("<td>" + html.escape(json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (list, dict)) else str(row[k] if row[k] is not None else "")) + "</td>" for k in fields) + "</tr>")
                    out.write("</tbody></table></html>")
                out.flush()
                os.fsync(out.fileno())
            finish(store, job, 'report:' + fmt, relative, temp, replace, checkpoint)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
                forget_temporary(store,job['root'],temp)
        paths.append(relative)
    return paths
