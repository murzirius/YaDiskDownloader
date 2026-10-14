import hashlib
import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone

from . import __version__
from .paths import publish, reserve_output, safe_join, temporary_file


SETTINGS = ("mode", "preview_size", "file_types", "workers", "attempts", "timeout", "reports", "create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts", "operation")


def error_kind(value):
    value = value.lower()
    for text, name in (("лимит попытки", "attempt_timeout"), ("таймаут", "network_timeout"), ("сетевая", "network"), ("места", "disk_space"), ("sha-256", "checksum"), ("sha256", "checksum"), ("md5", "checksum"), ("неполный", "incomplete_response"), ("размер", "size_mismatch"), ("недопустим", "validation"), ("токен", "authorization"), ("не подключ", "authorization"), ("недоступ", "unavailable")):
        if text in value:
            return name
    return "other" if value else "none"


def bundle(engine, job_id):
    status = engine.status(job_id)
    summary = {key: status.get(key) for key in ("phase", "files", "folders", "processed", "originals", "previews", "errors", "scan_errors", "cancelled", "replacements", "missing_previews", "saved_bytes", "downloaded", "zip_bytes", "partial_bytes", "free_bytes", "workers_status")}
    summary["error_type"] = error_kind(status["error"])
    settings = {key: status["settings"][key] for key in SETTINGS if key in status["settings"]}
    environment = {"app_version": __version__, "python": sys.version.split()[0], "system": platform.system(), "system_release": platform.release(), "architecture": platform.machine(), "created": datetime.now(timezone.utc).isoformat()}
    entries = []
    for row in engine.store.rows("SELECT path,status,error,original_status,preview_status,original_attempts,preview_attempts FROM entries WHERE job=? AND kind='file' AND (error!='' OR status IN ('error','partial','cancelled','no_preview')) LIMIT 1000", (job_id,)):
        row["source_id"] = hashlib.sha256(row.pop("path").encode()).hexdigest()[:16]
        row["error_type"] = error_kind(row.pop("error"))
        entries.append(row)
    events = []
    for row in engine.store.rows("SELECT time,category,level,message,path FROM journal_events WHERE job=? ORDER BY id DESC LIMIT 2000", (job_id,)):
        message, path = row.pop("message"), row.pop("path")
        row["error_type"] = error_kind(message) if row["category"] == "errors" else "none"
        match = re.search(r"\bHTTP (\d{3})\b", message)
        if match:
            row["http_status"] = int(match[1])
        match = re.search(r"попытка (\d+)/(\d+)", message)
        if match:
            row.update(attempt=int(match[1]), attempts=int(match[2]))
        if path:
            row["source_id"] = hashlib.sha256(path.encode()).hexdigest()[:16]
        events.append(row)
    return {"environment.json": environment, "settings.json": settings, "summary.json": summary, "errors.json": entries, "events.json": events}


def write_bundle(engine, job_id):
    job = engine.store.job(job_id)
    files = bundle(engine, job_id)
    local = reserve_output(engine.store, job, "diagnostics-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
    temp = temporary_file(job["root"])
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("README.txt", "Диагностика YaDiskDownloader. Без ключей, временных ссылок, имён и путей файлов, исходного содержимого, профилей и базы SQLite. До 1000 ошибок файлов и 2000 последних событий. Идентификаторы source_id связывают ошибки и события одного источника.".encode("utf-8"))
        publish(temp, safe_join(job["root"], local))
        return local
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
