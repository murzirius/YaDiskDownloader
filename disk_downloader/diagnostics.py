import hashlib
import json
import os
import platform
import re
import secrets
import sys
import sqlite3
from .sqlite_support import safe_wal
import zipfile
from datetime import datetime, timezone

from . import __version__, __build__
from .paths import forget_temporary, publish, reserve_output, safe_join, temporary_file


SETTINGS = ("mode", "preview_size", "file_types", "workers", "attempts", "timeout", "total_timeout", "reports", "create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts", "operation", "speed_limit_mib", "missing_policy")


def error_kind(value):
    value = value.lower()
    if 'sqlite_' in value:
        return 'local_database'
    for text, name in (("нет новых данных", "idle_timeout"), ("лимит попытки", "attempt_timeout"), ("таймаут", "network_timeout"), ("сетевая", "network"), ("места", "disk_space"), ("sha-256", "checksum"), ("sha256", "checksum"), ("md5", "checksum"), ("неполный", "incomplete_response"), ("размер", "size_mismatch"), ("недопустим", "validation"), ("токен", "authorization"), ("не подключ", "authorization"), ("недоступ", "unavailable")):
        if text in value:
            return name
    return "other" if value else "none"


def storage_event_code(message):
    if message.startswith('Накопитель снова доступен.'):
        return 'storage_resumed'
    if message.startswith('Накопитель ответил.'):
        return 'storage_confirming'
    if message.startswith(('Накопитель или NAS не отвечает', 'Проверка накопителя ещё выполняется', 'Накопитель ещё не ответил')):
        return 'storage_probe_wait'
    if message.startswith('Папка найдена, но не удалось проверить свободное место'):
        return 'storage_space_probe_failed'
    if message.startswith('Папка загрузки недоступна.'):
        return 'storage_unavailable'
    if message.startswith(('По выбранному пути теперь другая папка', 'Папка назначения больше не является обычной папкой')):
        return 'storage_validation_blocked'
    return None


def bundle(engine, job_id):
    salt = secrets.token_bytes(32)
    def source_id(path):
        return hashlib.sha256(salt + path.encode()).hexdigest()[:16]
    status = engine.status_cache.snapshot(engine.store.job(job_id))
    summary = {key: status.get(key) for key in ("phase", "files", "folders", "processed", "originals", "previews", "errors", "scan_errors", "cancelled", "replacements", "missing_previews", "saved_bytes", "downloaded", "zip_bytes", "partial_bytes", "free_bytes", "workers_status")}
    summary["error_type"] = error_kind(status["error"])
    summary['statistics_pending'] = bool(status.get('statistics_pending'))
    if summary['statistics_pending']:
        for key in summary:
            if key not in ('phase', 'error_type', 'statistics_pending'):
                summary[key] = None
    settings = {key: status["settings"][key] for key in SETTINGS if key in status["settings"]}
    environment = {"app_version": __version__, "app_build": __build__, "sqlite": sqlite3.sqlite_version, "safe_sqlite_wal": safe_wal(), "python": sys.version.split()[0], "system": platform.system(), "system_release": platform.release(), "architecture": platform.machine(), "created": datetime.now(timezone.utc).isoformat()}
    entries = []
    for row in engine.store.rows("SELECT path,status,error,original_status,preview_status,original_attempts,preview_attempts FROM entries WHERE job=? AND kind='file' AND (error!='' OR status IN ('error','cancelled','no_preview')) LIMIT 1000", (job_id,)):
        row["source_id"] = source_id(row.pop("path"))
        row["error_type"] = error_kind(row.pop("error"))
        entries.append(row)
    events = []
    for row in engine.store.rows("SELECT time,category,level,message,path FROM journal_events WHERE job=? ORDER BY id DESC LIMIT 2000", (job_id,)):
        message, path = row.pop("message"), row.pop("path")
        row["error_type"] = error_kind(message) if row["category"] == "errors" else "none"
        if row['category'] == 'system' and (code := storage_event_code(message)):
            row['event_code'] = code
            match = re.search(r'Код (Windows|ОС): (\d+)', message)
            if match:
                row['os_error_code'] = int(match[2])
                row['os_error_source'] = 'windows' if match[1]=='Windows' else 'os'
        match = re.search(r"\bHTTP (\d{3})\b", message)
        if match:
            row["http_status"] = int(match[1])
        match = re.search(r"попытка (\d+)/(\d+)", message)
        if match:
            row.update(attempt=int(match[1]), attempts=int(match[2]))
        if path:
            row["source_id"] = source_id(path)
        events.append(row)
    control = engine.controls.get(job_id)
    storage = control.storage_snapshot() if control else None
    # snapshot contains only fixed enums, counters and prewritten user messages.
    # Exclude the message as well: exports need no free-form text.
    if storage:
        storage.pop('message', None)
        if storage.get('probe'):
            storage['probe'] = dict(storage['probe'])
            storage['probe'].pop('error', None)
    return {"environment.json": environment, "settings.json": settings, "summary.json": summary, "errors.json": entries, "events.json": events,
            "storage.json": storage,
            "requests.json": engine.request_errors.snapshot() if hasattr(engine, 'request_errors') else []}


def write_bundle(engine, job_id):
    job = engine.store.job(job_id)
    files = bundle(engine, job_id)
    local = reserve_output(engine.store, job, "diagnostics-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
    temp = temporary_file(job["root"], engine.store, 'diagnostics')
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("README.txt", "Диагностика YaDiskDownloader. Без ключей, временных ссылок, имён и путей файлов, исходного содержимого, профилей и базы SQLite. До 1000 ошибок файлов и 2000 последних событий. storage.json содержит этап, время и числовой код ошибки проверки накопителя, без адреса NAS. Идентификаторы source_id связывают ошибки и события одного источника.".encode("utf-8"))
        with open(temp,'r+b') as stream:
            os.fsync(stream.fileno())
        publish(temp, safe_join(job["root"], local))
        return local
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
        forget_temporary(engine.store,job['root'],temp)
