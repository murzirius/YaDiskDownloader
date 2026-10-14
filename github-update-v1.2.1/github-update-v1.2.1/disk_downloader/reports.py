import csv
import html
import json
import os
from itertools import groupby

from .paths import publish, reserve_output, safe_join, temporary_file


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
    return {k: job[k] for k in keys}


def excel_cell(value):
    value = str(value) if value is not None else ""
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


def write_reports(store, job_id, checkpoint=lambda: None, final_phase=None):
    job = store.job(job_id)
    summary = context(store, job_id)
    if final_phase:
        summary["phase"] = final_phase
    generation = job["generation"] + 1
    fields = ["record_type", "source_path", "local_path", "status", "error", "original_attempt", "preview_attempt", "fallback_to_preview", "results", "original_attempts", "preview_attempts"]
    paths = []
    for fmt in job["settings"]["reports"]:
        relative = reserve_output(store, job, f"report-{generation}.{fmt}")
        final = safe_join(job["root"], relative)
        temp = temporary_file(job["root"])
        try:
            with open(temp, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as out:
                if fmt == "json":
                    out.write('{"summary":' + json.dumps(summary, ensure_ascii=False) + ',"entries":[')
                    first = True
                    for row in records(store, job_id):
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
                    for row in records(store, job_id):
                        checkpoint()
                        writer.writerow(["", *[excel_cell(json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (list, dict)) else row[k]) for k in fields]])
                elif fmt == "txt":
                    out.write("Локальная копия Яндекс.Диска — отчёт\n\nНастройки и итог:\n")
                    out.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n\nРесурсы:\n")
                    for row in records(store, job_id):
                        checkpoint()
                        out.write(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
                elif fmt == "html":
                    out.write('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Отчёт Яндекс.Диск</title><style>body{font:14px system-ui;margin:24px;background:#17191d;color:#e5e7eb}table{border-collapse:collapse;width:100%}th,td{border:1px solid #41454d;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere}th{background:#272b33}</style><h1>Отчёт Яндекс.Диск</h1><p>100% обработки может включать ошибки. Превью не является оригиналом.</p><pre>')
                    out.write(html.escape(json.dumps(summary, ensure_ascii=False, indent=2)))
                    out.write("</pre><table><thead><tr>" + "".join("<th>" + html.escape(k) + "</th>" for k in fields) + "</tr></thead><tbody>")
                    for row in records(store, job_id):
                        checkpoint()
                        out.write("<tr>" + "".join("<td>" + html.escape(json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (list, dict)) else str(row[k] if row[k] is not None else "")) + "</td>" for k in fields) + "</tr>")
                    out.write("</tbody></table></html>")
            publish(temp, final)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        paths.append(relative)
    return paths
