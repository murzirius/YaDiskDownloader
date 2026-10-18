import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

from .api import APIError, Deadline, NetworkError, PausedRequest, PREVIEW_MAX_BYTES, SIZES, Suspended, attempt_count, retry_delay, validate_resource
from .import_state import find_import, inherit, save_import
from .file_types import FILE_TYPES, file_type, preview_type, selected_type
from .estimation import RemainingTime
from .paths import file_signature, PathError, component, digest, native, normalize_selection, parent_path, publish, remote_path, reserve_output, safe_join, suffix, temporary_file
from .reports import write_reports, REPORT_LOCK
from .journal import Journal, PHASES
from .partials import Partials, strong_etag
from .folder_usage import FolderUsage
from . import exclusions, rate_limit
from .inventory import compare, write_changes
from .network import NetworkWait
from .limits import MAX_WORKERS
from .status_cache import StatusCache


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(meta):
    return hashlib.sha256(json.dumps({k: meta.get(k) for k in ("path", "size", "md5", "sha256", "modified", "resource_id")}, sort_keys=True).encode()).hexdigest()


def file_hash(filename, checkpoint=lambda: None):
    sha = hashlib.sha256()
    with open(filename, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checkpoint()
            sha.update(chunk)
    return sha.hexdigest()


def safe_error(exc):
    if isinstance(exc, NetworkError):
        detail = str(exc)
        if detail == "Сетевая ошибка или таймаут":
            return "Не удалось получить данные от Яндекс.Диска: соединение оборвалось или сервер не ответил. Проверьте интернет. Если другие сайты работают, повторите позже — Диск может временно ограничивать загрузку."
        return "Получение данных от Яндекс.Диска: " + detail + ". Можно повторить загрузку; сохранённые файлы не удаляются."
    if isinstance(exc, (APIError, PathError, Suspended)):
        return str(exc)
    if isinstance(exc, requests.RequestException):
        return "Сетевая ошибка или таймаут"
    if isinstance(exc, sqlite3.Error):
        return 'Ошибка базы истории (' + getattr(exc, 'sqlite_errorname', 'SQLITE_ERROR') + ')'
    if isinstance(exc, OSError):
        if exc.errno == 28 or getattr(exc, "winerror", None) == 112:
            return "Недостаточно места на диске ПК. Освободите место в папке загрузки и нажмите «Продолжить»."
        if isinstance(exc, PermissionError):
            return "Windows запретила запись или чтение файла. Закройте файл в других программах и проверьте доступ к папке загрузки. Затем повторите задание."
        if isinstance(exc, FileNotFoundError):
            return "Файл или папка на ПК не найдены. Проверьте, что диск подключён и папку загрузки не перенесли."
        return f"Ошибка файловой системы (код {exc.errno or getattr(exc, 'winerror', 0)})"
    return "Ошибка проверки или обработки файла"


class Control:
    def __init__(self):
        self.condition = threading.Condition()
        self.paused = False
        self.stopped = False
        self.active = {}
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.transferred = 0
        self.elapsed_before = 0
        self.pause_started = None
        self.finished = None
        self.temporary = set()
        self.temporary_sizes = {}
        self.journal = None
        self.network = NetworkWait(self.event, clock=self.active_clock)
        self.remaining_time = RemainingTime(self.statistics_clock)
        self.space_check = None
        self.space_lock = threading.Lock()
        self.next_space_check = 0
        self.pause_reason = ""
        self.adaptive = None
        self.selected_paths = None
        self.rate_limiter = rate_limit.RateLimiter(0)
        self.verification = None

    def event(self, group, message, path="", level="info"):
        if self.journal:
            self.journal.event(group, message, path, level)

    def check(self):
        if self.space_check and not self.paused:
            self.space_check()
        with self.condition:
            while self.paused and not self.stopped:
                self.condition.wait(0.2)
            if self.stopped:
                raise Suspended("Задание остановлено")

    def pause(self):
        with self.condition:
            if not self.paused:
                self.paused = True
                self.pause_started = time.monotonic()
        self.network.pause_requests()

    def resume(self):
        with self.condition:
            if self.pause_started is not None:
                self.elapsed_before += time.monotonic() - self.pause_started
                self.pause_started = None
            self.paused = False
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        self.network.abort_requests()

    def progress(self, path, done, size, action, **details):
        with self.lock:
            self.active[path] = {"path": path, "done": done, "size": size, "action": action, **details}

    def snapshot(self):
        with self.lock:
            elapsed = (self.finished or self.pause_started or time.monotonic()) - self.started - self.elapsed_before - self.network.elapsed
            return {"active": list(self.active.values()), "speed": self.transferred / max(0.1, elapsed), 'verification':dict(self.verification) if self.verification else None}

    def active_clock(self):
        with self.condition:
            current = self.finished or time.monotonic()
            return current - self.elapsed_before - (current - self.pause_started if self.pause_started is not None else 0)

    def statistics_clock(self):
        return self.active_clock() - self.network.elapsed


class Engine:
    def __init__(self, store, api):
        self.store = store
        self.api = api
        self.lock = threading.RLock()
        self.controls = {}
        self.threads = {}
        self.journal_interval = 300
        self.partials = Partials(store)
        self.shutting_down = False
        self.folder_usage = FolderUsage()
        self.maintenance_busy = False
        self.status_cache = StatusCache(self)

    def validate_settings(self, data):
        if "attempts" not in data and "retries" in data and (isinstance(data["retries"], bool) or not isinstance(data["retries"], int) or not 0 <= data["retries"] <= 8):
            raise ValueError("Некорректное число повторов")
        mode = data.get("mode", "previews")
        if mode not in ("previews", "both", "fallback"):
            raise ValueError("Некорректный режим загрузки")
        size = data.get("preview_size", "XXXL")
        if size not in SIZES:
            raise ValueError("Некорректный размер превью")
        reports = data.get("reports", ["csv", "json"])
        if not isinstance(reports, list) or not reports or any(r not in ("csv", "json", "html", "txt") for r in reports):
            raise ValueError("Выберите хотя бы один формат: CSV, JSON, HTML, TXT")
        file_types = data.get("file_types", list(FILE_TYPES))
        if not isinstance(file_types, list) or not file_types or any(category not in FILE_TYPES for category in file_types):
            raise ValueError("Выберите хотя бы один тип: фото, видео, музыка, другие файлы")
        paths = data.get("paths", [])
        if not isinstance(paths, list) or not isinstance(data.get("whole_disk", False), bool):
            raise ValueError("Некорректный список исходных путей")
        destination = data.get("destination", "")
        if not isinstance(destination, str) or not Path(destination).is_absolute():
            raise ValueError("Укажите абсолютный путь локальной папки назначения")
        destination = str(Path(destination).resolve())
        values = {}
        for name, default, low, high in (("workers", 8, 1, MAX_WORKERS), ("attempts", attempt_count(data), 1, 9), ("timeout", 20, 5, 3600), ("total_timeout", 0, 0, 86400)):
            value = data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                label = {"workers": "Число одновременных загрузок", "attempts": "Попытки для каждого файла", "timeout": "Ожидание без новых данных (секунды)", "total_timeout": "Общий предел попытки (секунды)"}[name]
                raise ValueError(f"{label}: укажите целое число от {low} до {high}.")
            values[name] = value
        operation = data.get("operation", "new")
        if operation not in ("new", "missing"):
            raise ValueError("Некорректный способ загрузки")
        missing_policy = data.get("missing_policy", "originals")
        if missing_policy not in ("originals", "any_result"):
            raise ValueError("Выберите, что считать скачанным: только оригинал или оригинал и превью.")
        values["missing_policy"] = missing_policy
        values["retries"] = values["attempts"] - 1
        for name, default in (("create_zip", True), ("auto_pause", True), ("resume_parts", True), ("adaptive_workers", False)):
            value = data.get(name, default)
            if not isinstance(value, bool):
                raise ValueError("Некорректная настройка " + name)
            values[name] = value
        fullness = data.get("max_disk_percent", 95)
        if isinstance(fullness, bool) or not isinstance(fullness, (int, float)) or not 1 <= fullness <= 99.9:
            raise ValueError("Порог заполнения диска: от 1 до 99,9%")
        values["adaptive_workers"] = False
        values["max_disk_percent"] = fullness
        values["exclusions"] = exclusions.validate(data.get("exclusions", []))
        values["speed_limit_mib"] = rate_limit.validate(data.get("speed_limit_mib", 0))
        return {"mode": mode, "preview_size": size, "reports": sorted(set(reports)), "file_types": sorted(set(file_types)),
                "paths": normalize_selection(paths, data.get("whole_disk", False)), "whole_disk": data.get("whole_disk", False),
                "destination": destination, "operation": operation, **values}

    def ensure_idle(self):
        recovery = getattr(self,'recovery',None)
        if recovery and (recovery.snapshot()['busy'] or recovery.snapshot()['error']):
            raise ValueError('Сначала дождитесь восстановления после сбоя или нажмите «Повторить восстановление». Сохранённые файлы проверяются на ПК.')
        if self.maintenance_busy or any(thread.is_alive() for thread in self.threads.values()):
            raise ValueError("Сейчас выполняется другая работа. Вернитесь к текущему заданию и нажмите «Остановить», затем дождитесь остановки. Скачанные файлы останутся; после этого можно подключить ключ или запустить другое задание.")

    def create(self, data):
        with self.lock:
            self.ensure_idle()
            return self._create(data)

    def _create(self, data):
        if self.shutting_down:
            raise ValueError("Приложение завершается")
        if not self.api.credentials.connected:
            raise ValueError("Сначала подключите Диск")
        data = dict(data)
        previous = None
        if data.get("operation") == "missing":
            root = Path(data.get("destination", ""))
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("Выберите существующую папку предыдущей загрузки")
            previous = find_import(self.store, root)
            if previous and "file_types" not in data:
                data["file_types"] = previous["job"]["settings"].get("file_types", list(FILE_TYPES))
            if previous and not data.get("paths") and not data.get("whole_disk"):
                data["paths"] = previous["job"]["settings"]["paths"]
                data["whole_disk"] = previous["job"]["settings"].get("whole_disk", False)
        settings = self.validate_settings(data)
        if previous:
            settings["comparison_available"] = bool(previous.get("store") or previous.get("inventory_available"))
            settings['_inherit_source'] = {'job':previous['id']} if previous.get('store') else {'manifest':previous['file'].name}
        job_id = uuid.uuid4().hex
        root = Path(settings["destination"]) if settings["operation"] == "missing" else Path(settings["destination"]) / ("Disk-" + job_id[:12])
        if settings["operation"] == "new":
            os.makedirs(native(root), exist_ok=False)
        service = previous["job"]["service"] if previous else "__disk_service_" + job_id[:12]
        safe_join(root, service)
        generation = previous["job"]["generation"] if previous else 0
        if previous and previous["job"]["settings"].get("manifest_local"):
            settings["manifest_local"] = previous["job"]["settings"]["manifest_local"]
        with self.store.db() as db:
            db.execute("INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)", (job_id, json.dumps(settings, ensure_ascii=False), str(root), service, "new", now()))
            db.execute("INSERT INTO names(job,identity,local,folded) VALUES(?,?,?,?)", (job_id, "@service", service, service.casefold()))
            db.execute("UPDATE jobs SET generation=? WHERE id=?", (generation, job_id))
        try:
            self.launch(job_id, "scan")
        except Exception:
            with self.store.db() as db:
                for table in ("entries", "names", "artifacts", "partials", "partial_ownership", "baseline", "changes", "verification_cache", "scan_errors", "journal_events"):
                    db.execute("DELETE FROM " + table + " WHERE job=?", (job_id,))
                db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self.controls.pop(job_id, None)
            self.threads.pop(job_id, None)
            if settings["operation"] == "new":
                try:
                    os.rmdir(native(root))
                except OSError:
                    pass
            raise
        return job_id

    def launch(self, job_id, operation="download", paths=None):
        with self.lock:
            if self.shutting_down:
                raise ValueError("Приложение завершается")
            if any(t.is_alive() for t in self.threads.values()):
                raise ValueError("Дождитесь завершения текущей операции или остановите её")
            if operation not in ('pack','manifest','journal') and not self.api.credentials.connected:
                raise ValueError("Подключите Диск перед продолжением")
            control = Control()
            control.journal = Journal(self, job_id, self.journal_interval)
            job = self.store.job(job_id)
            if job["settings"].get("_invalid"):
                raise ValueError("Настройки задания повреждены. Создайте дозагрузку в прежнюю папку.")
            if operation in ('scan', 'download'):
                job['settings']['scan_only_pending'] = operation == 'scan'
                self.store.update_job(job_id, settings=json.dumps(job['settings'], ensure_ascii=False))
            control.adaptive = None
            control.rate_limiter = rate_limit.RateLimiter(job["settings"].get("speed_limit_mib", 0), control.active_clock)
            control.space_check = lambda: self.check_space(job_id, control)
            control.selected_paths = tuple(paths) if paths else None
            control.reuse_scanned = operation == "download" and not paths and (job["settings"].get("operation") == "missing" or job["phase"] in ("paused", "stopped", "interrupted", "failed") or job["generation"] > 0)
            if operation == 'download' and not paths:
                with self.store.db() as db:
                    db.execute("UPDATE job_queue SET state='running',error='' WHERE job=?", (job_id,))
            self.controls[job_id] = control
            thread = threading.Thread(target=self.run, args=(job_id, operation, control), daemon=True)
            self.threads[job_id] = thread
            thread.start()

    def action(self, job_id, action, limits=None):
        if action in ('stop', 'pause'):
            control = self.controls.get(job_id)
            thread = self.threads.get(job_id)
            if control and thread and thread.is_alive():
                if action == 'stop':
                    control.stop()
                elif not control.stopped:
                    control.pause()
        with self.lock:
            job = self.store.job(job_id)
            scan_only = action == 'continue_scan'
            if scan_only:
                if job['scan_complete'] and not job['settings'].get('scan_only_pending'):
                    raise ValueError('Обход уже завершён. Нажмите «Начать скачивание».')
                action = 'resume'
            if job["settings"].get("_invalid") and action not in ("stop",):
                raise ValueError("Настройки задания повреждены. Создайте дозагрузку в прежнюю папку.")
            if action in ("start", "resume", "retry", "retry_selected", "pack", "save_manifest", "refresh_journal"):
                own = self.threads.get(job_id)
                if not (action == "resume" and own and own.is_alive() and self.controls[job_id].paused):
                    self.ensure_idle()
            live = self.threads.get(job_id)
            control = self.controls.get(job_id)
            if action == "pause":
                if not live or not live.is_alive() or control.stopped:
                    raise ValueError("Нет активной операции для паузы")
                control.pause()
                control.pause_reason = ""
                self.store.update_job(job_id, phase="paused", pause_reason="")
                control.event("system", "Задание поставлено на паузу")
                return
            if action == "stop":
                if live and live.is_alive():
                    control.stop()
                    with self.store.db() as db:
                        db.execute("UPDATE jobs SET phase='stopping' WHERE id=? AND phase IN ('new','scanning','preparing','downloading','packing','paused')", (job_id,))
                    control.event("system", "Запрошена остановка задания", level="warning")
                else:
                    self.store.update_job(job_id, phase="stopped", ended=now())
                    with self.store.db() as db:
                        db.execute("UPDATE entries SET status='cancelled' WHERE job=? AND kind='file' AND status IN ('pending','running')", (job_id,))
                    if control and control.journal:
                        control.event("system", "Задание остановлено", level="warning")
                        control.journal.flush()
                return
            if action == "resume" and live and live.is_alive() and control.paused:
                if limits and any(limits.get(k, job["settings"].get(k, 0 if k == "speed_limit_mib" else None)) != job["settings"].get(k, attempt_count(job["settings"]) if k == "attempts" else 0 if k == "speed_limit_mib" else None) for k in ("workers", "attempts", "timeout", "total_timeout", "speed_limit_mib")):
                    raise ValueError("Для изменения ограничений остановите задание и продолжите с новыми параметрами")
                if not self.api.credentials.connected and getattr(control, "phase", "downloading") != "packing":
                    raise ValueError("Для продолжения загрузки подключите Яндекс.Диск: вставьте ключ доступа вверху страницы. Скачанные файлы сохранены.")
                if limits and limits.get("missing_policy", job["settings"].get("missing_policy", "originals")) != job["settings"].get("missing_policy", "originals"):
                    raise ValueError("Чтобы изменить учёт превью, нажмите «Остановить», дождитесь остановки и затем «Продолжить».")
                if limits:
                    keys = ("create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts", "speed_limit_mib")
                    validated = self.validate_settings(job["settings"] | {key: limits[key] for key in keys if key in limits})
                    job["settings"].update({key: validated[key] for key in keys})
                    self.store.update_job(job_id, settings=json.dumps(job["settings"], ensure_ascii=False))

                if control.pause_reason and self.check_space(job_id, control, force=True):
                    raise ValueError(control.pause_reason)
                self.store.update_job(job_id, phase=getattr(control, "phase", "downloading"), pause_reason="")
                control.pause_reason = ""
                control.resume()
                control.event("system", "Работа продолжена после паузы")
                return
            if action in ("start", "resume", "retry", "retry_selected"):
                if live and live.is_alive():
                    raise ValueError("Операция ещё выполняется")
                if action == "retry_selected":
                    paths = (limits or {}).get("paths")
                    if not isinstance(paths, list) or not paths or len(paths) > 1000 or any(not isinstance(p, str) for p in paths):
                        raise ValueError("Выберите от 1 до 1000 файлов для повтора")
                    paths = sorted(set(remote_path(path) for path in paths))
                    with self.store.db() as db:
                        found = db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND kind='file' AND path IN (" + ",".join("?" for _ in paths) + ")", (job_id, *paths)).fetchone()[0]
                    if found != len(paths):
                        raise ValueError("Выбранный файл не принадлежит заданию")
                if limits:
                    keys = ("workers", "attempts", "timeout", "total_timeout", "create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts", "speed_limit_mib", "missing_policy")
                    validated = self.validate_settings({**job["settings"], **{k: limits[k] for k in keys if k in limits}})
                    job["settings"].update({k: validated[k] for k in (*keys, "retries")})
                    self.store.update_job(job_id, settings=json.dumps(job["settings"], ensure_ascii=False))
                with self.store.db() as db:
                    if action == "retry_selected":
                        paths = (limits or {}).get("paths")
                        if not isinstance(paths, list) or not paths or len(paths) > 1000 or any(not isinstance(p, str) for p in paths):
                            raise ValueError("Выберите от 1 до 1000 файлов для повтора")
                        paths = sorted(set(remote_path(path) for path in paths))
                        placeholders = ",".join("?" for _ in paths)
                        found = db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND kind='file' AND path IN (" + placeholders + ")", (job_id, *paths)).fetchone()[0]
                        if found != len(paths):
                            raise ValueError("Выбранный файл не принадлежит заданию")
                        db.execute("UPDATE entries SET status='pending',error='' WHERE job=? AND kind='file' AND path IN (" + placeholders + ")", (job_id, *paths))
                    elif action == "retry":
                        db.execute("UPDATE entries SET status='pending', error='' WHERE job=? AND kind='file' AND (status IN ('error','partial','no_preview','cancelled') OR fallback=1 OR (?='originals' AND EXISTS(SELECT 1 FROM artifacts a WHERE a.job=entries.job AND a.path=entries.path AND a.type='preview') AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.job=entries.job AND a.path=entries.path AND a.type='original')))", (job_id, job["settings"].get("missing_policy", "originals")))
                        db.execute("UPDATE entries SET scanned=0 WHERE job=? AND kind='dir' AND path IN (SELECT path FROM scan_errors WHERE job=?)", (job_id, job_id))
                        has_scan_errors = db.execute("SELECT COUNT(*) FROM scan_errors WHERE job=?", (job_id,)).fetchone()[0]
                        if has_scan_errors:
                            db.execute("UPDATE jobs SET scan_complete=0 WHERE id=?", (job_id,))
                    else:
                        if job["phase"] == "ready" and job["settings"].get("operation") == "missing":
                            db.execute("UPDATE entries SET status='pending' WHERE job=? AND kind='file' AND NOT (status='existing' AND (original_status='existing_verified' OR (?='any_result' AND preview_status='existing_verified')))", (job_id, job["settings"].get("missing_policy", "originals")))
                        else:
                            db.execute("UPDATE entries SET status='pending' WHERE job=? AND kind='file'", (job_id,))
                self.launch(job_id, "scan" if scan_only else "download", paths=paths if action == "retry_selected" else None)
                return
            if action == "pack":
                self.launch(job_id, "pack")
                return
            if action == 'save_manifest':
                self.launch(job_id,'manifest')
                return
            if action == 'refresh_journal':
                self.launch(job_id,'journal')
                return
            raise ValueError("Неизвестная команда")

    def disconnect(self):
        for control in tuple(self.controls.values()):
            control.stop()
        self.api.credentials.clear()

    def reconcile(self, job_id):
        """A dead worker must leave a task that can be resumed, never a busy button."""
        job = self.store.job(job_id)
        if not self.lock.acquire(blocking=False):
            return job
        try:
            thread = self.threads.get(job_id)
            # The worker may have finished between the first read and this lock.
            # Re-read before changing its durable phase.
            if not thread or not thread.is_alive():
                job = self.store.job(job_id)
            if (not thread or not thread.is_alive()) and job["phase"] in ("new", "scanning", "preparing", "downloading", "packing", "paused", "pausing", "stopping"):
                self.store.update_job(job_id, phase="interrupted", error=job["error"] or "Работа приложения прервалась. Файлы и прогресс сохранены. Подключите Яндекс.Диск и нажмите «Продолжить».")
                job = self.store.job(job_id)
            return job
        finally:
            self.lock.release()

    def status(self, job_id):
        self.reconcile(job_id)
        live = bool(self.threads.get(job_id) and self.threads[job_id].is_alive())
        status = self.store.summary(job_id)
        control = self.controls.get(job_id)
        status.update(control.snapshot() if control else {"active": [], "speed": 0})
        status["live"] = live
        if not live or control and control.paused:
            status["speed"] = 0
        finishing = status["phase"] in ("completed", "completed_with_errors", "stopped", "failed", "ready")
        if live and control and not finishing:
            if control.stopped:
                status["phase"] = "stopping"
            elif control.paused:
                status["phase"] = "paused"
        status["can_pause"] = bool(live and control and not finishing and not control.stopped and not control.paused)
        status["can_resume"] = bool(not live or control and control.paused and not control.stopped)
        status["network_waiting"] = bool(live and control and control.network.offline.is_set())
        status["network_warning"] = control.network.message if status["network_waiting"] else ""
        if status["network_waiting"]:
            status["speed"] = 0
        status["journal_saved"] = control.journal.saved if control and control.journal else None
        status["journal_error"] = control.journal.error if control and control.journal else ""
        status["pause_reason"] = control.pause_reason if control else status.get("pause_reason", "")
        status["workers_status"] = control.adaptive.snapshot() if control and control.adaptive else {"current": status["settings"]["workers"], "maximum": status["settings"]["workers"], "enabled": False, "reason": ""}
        status["partial_bytes"] = 0
        service_bytes = 0
        counted_outputs = set()
        for row in self.store.rows("SELECT local FROM names WHERE job=? AND identity LIKE '@output:%'", (job_id,)):
            try:
                service_bytes += os.path.getsize(safe_join(status["root"], row["local"]))
                counted_outputs.add(row["local"])
            except (OSError, PathError):
                pass
        if status["zip_path"] and status["zip_path"] not in counted_outputs:
            try:
                service_bytes += os.path.getsize(safe_join(status["root"], status["zip_path"]))
            except (OSError, PathError):
                pass
        if control:
            with control.lock:
                temporary = tuple((filename, control.temporary_sizes.get(filename, 0)) for filename in control.temporary)
            for filename, written in temporary:
                try:
                    actual = os.path.getsize(filename)
                except FileNotFoundError:
                    continue
                except OSError:
                    actual = 0
                service_bytes += max(actual, written)
        active_temporary = {name for name, written in temporary} if control else set()
        for part in self.store.rows("SELECT local FROM partials WHERE job=?", (job_id,)):
            try:
                filename = safe_join(status["root"], part["local"])
                size = os.path.getsize(filename)
                status["partial_bytes"] += size
                if filename not in active_temporary:
                    service_bytes += size
            except (OSError, PathError):
                pass
        status["local_bytes"] = status["saved_bytes"] + service_bytes
        usage = self.folder_usage.snapshot(status["root"], active=live and not (control and control.paused))
        status["folder_bytes"] = usage['bytes'] if usage['bytes'] is not None else None if usage.get('error') else status['local_bytes']
        status['folder_bytes_error'] = usage.get('error','')
        status["folder_bytes_scanning"] = usage["scanning"]
        status["folder_bytes_updated"] = usage["updated"]
        status["eta_seconds"] = None
        status["eta_phase"] = None
        status["eta_message"] = "Ожидание интернета" if status["network_waiting"] else "Уточняется…"
        status["eta_basis"] = None
        if live and control and not status["network_waiting"] and status["phase"] in ("downloading", "packing"):
            phase = status["phase"]
            work = status["remaining_work"]
            remaining_bytes, unknown_files = work["bytes"], work["unknown_files"]
            if status["settings"]["mode"] == "both" and status["settings"].get("operation") != "missing" and not status["generation"]:
                unknown_files = max(unknown_files, work["preview_files"])
            with control.lock:
                for item in status["active"]:
                    if item.get("kind") == "original":
                        if item.get("source_size") is None and item["size"] is not None:
                            remaining_bytes += max(0, item["size"] - item["done"])
                            unknown_files = max(0, unknown_files - 1)
                        else:
                            remaining_bytes = max(0, remaining_bytes - item["done"])
                    elif item.get("kind") == "preview" and item["size"] is not None:
                        remaining_bytes += max(0, item["size"] - item["done"])
                        unknown_files = max(0, unknown_files - 1)
                if phase == "packing":
                    status["eta_seconds"] = control.remaining_time.estimate(phase, status["zip_done"], 0, max(0, status["zip_total"] - status["zip_done"]), 0)
                else:
                    original_only = status["settings"]["mode"] == "fallback" or status["settings"].get("operation") == "missing" or status["generation"] > 0
                    status["eta_seconds"] = control.remaining_time.estimate(phase, control.transferred, status["processed"], remaining_bytes, work["files"], unknown_files, original_only=original_only)
                status["eta_message"] = control.remaining_time.reason
                status["eta_basis"] = control.remaining_time.basis
            status["eta_phase"] = phase
        elif status["phase"] in ("completed", "completed_with_errors") and not live:
            status["eta_seconds"] = 0
        try:
            status["free_bytes"] = shutil.disk_usage(status["root"]).free
        except OSError:
            status["free_bytes"] = None
        return status

    def phase(self, job_id, control, phase):
        control.phase = phase
        if phase in ("downloading", "packing"):
            control.remaining_time.start(phase, 0 if phase == "packing" else control.transferred, self.store.summary(job_id)["processed"])
        self.store.update_job(job_id, phase="paused" if control.paused else phase)
        control.event("archive" if phase == "packing" else "system", PHASES.get(phase, phase))

    def check_space(self, job_id, control, force=False):
        with control.space_lock:
            if not force and time.monotonic() < control.next_space_check:
                return bool(control.pause_reason)
            control.next_space_check = time.monotonic() + 0.5
            job = self.store.job(job_id)
            if not job["settings"].get("auto_pause", True):
                return False
            usage = shutil.disk_usage(job["root"])
            occupied = (usage.total - usage.free) * 100 / max(1, usage.total)
            threshold = job["settings"].get("max_disk_percent", 95)
            if occupied >= threshold or usage.free < 8 * 1024 * 1024:
                reason = f"Автопауза: диск заполнен на {occupied:.1f}%, порог {threshold:g}%. Освободите место и нажмите «Продолжить»."
                if not control.pause_reason:
                    control.pause_reason = reason
                    control.pause()
                    self.store.update_job(job_id, phase="paused", pause_reason=reason)
                    control.event("system", reason, level="warning")
                return True
            return False

    def run(self, job_id, operation, control):
        try:
            with self.store.activity():
                self._run(job_id, operation, control)
        except Exception as exc:
            # Includes failures opening the activity connection / final journal.
            # The next status request also reconciles a dead worker's phase.
            try:
                self.store.update_job(job_id, phase="failed", ended=now(), error=safe_error(exc))
            except sqlite3.Error:
                pass
            finally:
                with control.lock:
                    control.active.clear()
                    control.finished = time.monotonic()

    def _run(self, job_id, operation, control):
        saved_scan = False
        try:
            previous_job = self.store.job(job_id)
            self.store.update_job(job_id, error="", ended=None, pause_reason="")
            job = self.store.job(job_id)
            if job['settings'].get('_inherit_source'):
                self.phase(job_id, control, 'preparing')
                source = job['settings']['_inherit_source']
                if source.get('job'):
                    previous = {'job':self.store.job(source['job']),'store':self.store,'id':source['job']}
                else:
                    previous = find_import(self.store, Path(job['root']), exclude_job=job_id)
                if not previous:
                    raise ValueError('На ПК не найдена прежняя опись загрузки. Проверьте папку или создайте дозагрузку заново.')
                inherit(self.store,job_id,previous,control.check,lambda count:control.progress(job['root'],count,None,'Читаем историю скачанной папки',unit='objects'))
                settings = self.store.job(job_id)['settings']
                settings.pop('_inherit_source',None)
                self.store.update_job(job_id,settings=json.dumps(settings,ensure_ascii=False))
                with control.lock: control.active.clear()
            if operation in ('manifest','journal'):
                self.phase(job_id,control,'preparing')
                control.progress(job_id,0,None,'Обновляем опись на ПК' if operation=='manifest' else 'Обновляем HTML-журнал')
            control.journal.start(operation)
            if operation == 'journal':
                control.check()
                self.store.update_job(job_id,phase=previous_job['phase'],error=previous_job['error'],ended=previous_job['ended'])
                saved_scan=True
                return
            if operation == 'manifest':
                self.phase(job_id,control,'preparing')
                save_import(self.store,job_id,control.check)
                saved_scan=True
                summary=self.store.summary(job_id)
                phase='stopped' if not summary['scan_complete'] else 'ready' if self.store.job(job_id)['settings'].get('scan_only_pending') else 'completed_with_errors' if summary['errors'] or summary['scan_errors'] else 'completed' if summary['processed']==summary['files'] else 'stopped'
                self.store.update_job(job_id,phase=phase,ended=now())
                control.event('system','Опись в папке на ПК обновлена',level='success')
                return
            if operation == "pack":
                self.prepare_paths(job_id, control)
                job = self.store.job(job_id)
                self.store.update_job(job_id, ended=now(), phase="stopped" if not job["scan_complete"] else job["phase"])
                self.pack(job_id, control, manual=True)
                return
            if not self.store.job(job_id)["scan_complete"] and not control.selected_paths:
                self.phase(job_id, control, "scanning")
                self.scan(job_id, control)
            self.phase(job_id, control, "preparing")
            self.prepare_paths(job_id, control, prime=operation == "scan" or getattr(control, "reuse_scanned", False))
            if operation == "scan":
                save_import(self.store, job_id, control.check)
                saved_scan = True
                self.store.update_job(job_id, phase="ready")
                return
            self.phase(job_id, control, "downloading")
            self.download(job_id, control)
            self.store.update_job(job_id, ended=now())
            self.pack(job_id, control)
        except Suspended as exc:
            with self.store.db() as db:
                db.execute("UPDATE entries SET status='cancelled' WHERE job=? AND kind='file' AND status IN ('pending','running')", (job_id,))
            self.store.update_job(job_id, phase="stopped", ended=now(), error=str(exc))
            control.event("system", str(exc), level="warning")
        except BaseException as exc:
            with self.store.db() as db:
                db.execute("UPDATE entries SET status='pending' WHERE job=? AND status='running'", (job_id,))
            self.store.update_job(job_id, phase="failed", ended=now(), error=safe_error(exc))
            control.event("errors", safe_error(exc), level="error")
        finally:
            control.event("system", PHASES.get(self.store.job(job_id)["phase"], "Операция завершена"))
            control.journal.close()
            try:
                if not saved_scan and not self.store.job(job_id)['settings'].get('_inherit_source'):
                    save_import(self.store, job_id)
            except (OSError, sqlite3.Error, ValueError) as exc:
                warning = 'Не удалось обновить опись в папке на ПК. Файлы и история сохранены в приложении. Проверьте доступ к папке и свободное место; затем повторите сохранение описи.'
                control.journal.error = warning
                self.store.update_job(job_id, error=warning)
                if self.store.job(job_id)['phase'] == 'completed':
                    self.store.update_job(job_id, phase='completed_with_errors')
            with control.lock:
                control.active.clear()
                control.finished = time.monotonic()

            # One final sweep includes the last media file, reports and manifest.
            # Idle history polling does not need to rescan the entire folder.
            if not self.shutting_down and operation != 'scan':
                self.folder_usage.snapshot(self.store.job(job_id)['root'],force=True)

    def scan(self, job_id, control):
        job = self.store.job(job_id)
        settings = job["settings"]
        for path in settings["paths"]:
            control.check()
            if exclusions.excluded(path, settings):
                continue
            with self.store.db() as db:
                exists = db.execute("SELECT 1 FROM entries WHERE job=? AND path=?", (job_id, path)).fetchone()
            if exists:
                continue
            try:
                meta = self.api.metadata(path, settings, control.check, limit=1)
                with self.store.db() as db:
                    self.store.insert_entry(db, job_id, meta, settings)
                    db.execute("DELETE FROM scan_errors WHERE job=? AND path=?", (job_id, path))
            except APIError as exc:
                if exc.status == 401:
                    raise Suspended(str(exc))
                with self.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO scan_errors VALUES(?,?,?)", (job_id, path, safe_error(exc)))
                control.event("errors", "Не удалось прочитать источник: " + safe_error(exc), path, "error")
        while True:
            control.check()
            with self.store.db() as db:
                directory = db.execute("SELECT path,scan_offset FROM entries WHERE job=? AND kind='dir' AND scanned=0 ORDER BY path LIMIT 1", (job_id,)).fetchone()
            if not directory:
                break
            path, offset = directory
            control.progress(path, offset, None, "Обход папки", unit="objects")
            try:
                meta = self.api.metadata(path, settings, control.check, offset=offset, limit=200)
                embedded = meta.get("_embedded")
                if not isinstance(embedded, dict) or not isinstance(embedded.get("items"), list):
                    raise APIError("API не вернул содержимое папки")
                items = embedded["items"]
                total = embedded.get("total")
                if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
                    raise APIError("API вернул некорректное число объектов папки")
                if not items and total is not None and offset < total:
                    raise APIError("Пустая страница до окончания списка API")
                with self.store.db() as db:
                    for item in items:
                        validate_resource(item)
                        child = remote_path(item.get("path", ""))
                        if parent_path(child) != path or item.get("type") not in ("file", "dir"):
                            raise APIError("API вернул недопустимый дочерний путь")
                        self.store.insert_entry(db, job_id, item, settings)
                    next_offset = offset + len(items)
                    total = embedded.get("total")
                    done = not items or (isinstance(total, int) and next_offset >= total)
                    db.execute("UPDATE entries SET scan_offset=?,scanned=? WHERE job=? AND path=?", (next_offset, int(done), job_id, path))
                    db.execute("DELETE FROM scan_errors WHERE job=? AND path=?", (job_id, path))
                control.event("scan", f"Получена страница папки: {len(items)} объектов; просмотрено {next_offset}" + (" · обход папки завершён" if done else ""), path)
            except (APIError, PathError) as exc:
                if isinstance(exc, APIError) and exc.status == 401:
                    raise Suspended(str(exc))
                with self.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO scan_errors VALUES(?,?,?)", (job_id, path, safe_error(exc)))
                    db.execute("UPDATE entries SET scanned=1 WHERE job=? AND path=?", (job_id, path))
                control.event("errors", "Ошибка обхода папки: " + safe_error(exc), path, "error")
        with control.lock:
            control.active.clear()
        self.store.update_job(job_id, scan_complete=1)
        control.check()
        self.phase(job_id, control, "preparing")
        control.progress(settings["paths"][0], 0, None, "Сравнение обходов", unit="objects")
        compare(self.store, job_id, control.check)

    def allocate(self, job_id, identity, proposed):
        with self.store.db() as db:
            existing = db.execute("SELECT local FROM names WHERE job=? AND identity=?", (job_id, identity)).fetchone()
            if existing:
                return existing[0]
            candidate = proposed
            counter = 0
            while db.execute("SELECT 1 FROM names WHERE job=? AND folded=?", (job_id, candidate.casefold())).fetchone():
                counter += 1
                parent, _, name = proposed.rpartition("/")
                name = suffix(name, digest(identity + str(counter)))
                candidate = parent + "/" + name if parent else name
            db.execute("INSERT INTO names VALUES(?,?,?,?)", (job_id, identity, candidate, candidate.casefold()))
            return candidate

    def map_path(self, job_id, path):
        if path == "disk:/":
            return ""
        source = "disk:/"
        local = ""
        for part in path[6:].split("/"):
            source = source.rstrip("/") + "/" + part
            proposed = (local + "/" if local else "") + component(part, source)
            local = self.allocate(job_id, source, proposed)
        return local

    def prepare_paths(self, job_id, control=None, prime=False):
        job = self.store.job(job_id)
        cursor = ""
        count = 0
        cache = {"disk:/": ""}
        while True:
            if control:
                control.check()
            with self.store.db() as db:
                rows = db.execute("SELECT path,kind,meta,local FROM entries WHERE job=? AND path>? AND (local IS NULL OR kind='dir') ORDER BY path COLLATE BINARY LIMIT 500", (job_id, cursor)).fetchall()
                if not rows:
                    break
                directories = []
                for entry in rows:
                    relative = entry["local"]
                    if relative is None:
                        source, relative = "disk:/", ""
                        for part in entry["path"][6:].split("/") if entry["path"] != "disk:/" else []:
                            source = source.rstrip("/") + "/" + part
                            if source in cache:
                                relative = cache[source]
                                continue
                            existing = db.execute("SELECT local FROM names WHERE job=? AND identity=?", (job_id, source)).fetchone()
                            if existing:
                                relative = existing[0]
                            else:
                                proposed = (relative + "/" if relative else "") + component(part, source)
                                relative, counter = proposed, 0
                                while db.execute("SELECT 1 FROM names WHERE job=? AND folded=?", (job_id, relative.casefold())).fetchone():
                                    counter += 1
                                    parent, _, name = proposed.rpartition("/")
                                    relative = (parent + "/" if parent else "") + suffix(name, digest(source + str(counter)))
                                db.execute("INSERT INTO names VALUES(?,?,?,?)", (job_id, source, relative, relative.casefold()))
                            if len(cache) > 8192:
                                cache = {"disk:/": ""}
                            cache[source] = relative
                        db.execute("UPDATE entries SET local=? WHERE job=? AND path=?", (relative, job_id, entry["path"]))
                    if entry["kind"] == "file":
                        meta = json.loads(entry["meta"])
                        db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND fingerprint!=?", (job_id, entry["path"], fingerprint(meta)))
                        if not preview_type(meta):
                            db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", (job_id, entry["path"]))
                    else:
                        directories.append(relative)
            for relative in directories:
                if control:
                    control.check()
                os.makedirs(safe_join(job["root"], relative), exist_ok=True)
            count += len(rows)
            cursor = rows[-1]["path"]
            if control:
                control.progress(job["settings"]["paths"][0], count, None, "Подготовка локальных путей", unit="objects")
        self.prioritize(job_id, control, prime=prime)
        if control:
            with control.lock:
                control.active.clear()

    def download(self, job_id, control):
        workers = self.store.job(job_id)["settings"]["workers"]
        first_stage_work = False
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="disk-download") as pool:
            for stage in range(2):
                if stage and first_stage_work:
                    with control.lock:
                        control.remaining_time.new_stage()
                condition = "" if stage else " AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.job=entries.job AND a.path=entries.path)"
                parameters = (job_id,)
                if control.selected_paths:
                    condition += " AND path IN (" + ",".join("?" for _ in control.selected_paths) + ")"
                    parameters += control.selected_paths
                active = set()
                while True:
                    control.check()
                    capacity = (control.adaptive.limit() if control.adaptive else workers) - len(active)
                    if capacity > 0:
                        with self.store.db() as db:
                            entries = db.execute("SELECT * FROM entries WHERE job=? AND kind='file' AND status='pending'" + condition + " ORDER BY priority,path LIMIT ?", (*parameters, capacity)).fetchall()
                            db.executemany("UPDATE entries SET status='running' WHERE job=? AND path=?", ((job_id, entry['path']) for entry in entries))
                        for entry in entries:
                            if not stage:
                                first_stage_work = True
                            active.add(pool.submit(self.process, job_id, dict(entry), control))
                    if not active:
                        break
                    done, active = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                    if control.adaptive:
                        control.adaptive.measure(control.transferred, len(active) + len(done))
                    for future in done:
                        future.result()

    def prioritize(self, job_id, control, prime=False):
        job = self.store.job(job_id)
        if prime and (job["settings"].get("operation") == "missing" or job["generation"] > 0 or getattr(control, "reuse_scanned", False)):
            self.check_local_results(job, control)
            return
        rows = self.store.rows("SELECT e.path,e.meta,e.status,a.type,a.local,a.size,a.sha256,a.fingerprint,v.signature FROM entries e JOIN artifacts a ON a.job=e.job AND a.path=e.path LEFT JOIN verification_cache v ON v.job=a.job AND v.local=a.local WHERE e.job=? AND e.kind='file' AND (? OR e.status='pending') ORDER BY e.path,a.type", (job_id, prime))
        candidates = {}
        def flush_candidates():
            with self.store.db() as db:
                for path, value in candidates.items():
                    db.execute("UPDATE entries SET priority=? WHERE job=? AND path=?", (value["priority"], job_id, path))
                    if prime and (job["settings"].get("operation") == "missing" or job["generation"] > 0):
                        if value["original"]:
                            db.execute("UPDATE entries SET status='existing',error='',original_status='existing_verified',preview_status='not_needed',fallback=0 WHERE job=? AND path=?", (job_id, path))
                            db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", (job_id, path))
                        elif value["status"] == "existing":
                            db.execute("UPDATE entries SET status='pending' WHERE job=? AND path=?", (job_id, path))
            candidates.clear()
        for index, row in enumerate(rows):
            if len(candidates)>=200 and row['path'] not in candidates:
                flush_candidates()
            if control:
                control.check()
                if index % 250 == 0:
                    control.progress(job["settings"]["paths"][0], index, None, "Учёт прежних результатов", unit="objects")
            meta = json.loads(row["meta"])
            valid, confirmed = False, False
            try:
                filename = safe_join(job["root"], row["local"])
                info = os.stat(filename)
                valid = os.path.isfile(filename) and info.st_size == row["size"] and row["fingerprint"] == fingerprint(meta)
                if row["type"] == "original" and meta.get("sha256"):
                    valid = valid and row["sha256"].lower() == meta["sha256"].lower()
                signature = file_signature(filename, row["sha256"])
                encoded = json.dumps(signature)
                confirmed = valid and signature[-1] is not None and row["signature"] == encoded
                if row["signature"] and not confirmed:
                    previous_signature = json.loads(row["signature"])
                    valid = valid and isinstance(previous_signature, list) and len(previous_signature) == 6 and previous_signature == list(signature[:6])
            except (OSError, ValueError):
                pass
            value = candidates.setdefault(row["path"], {"priority": 0, "original": False, "status": row["status"]})
            if valid:
                value["priority"] = max(value["priority"], 2 if row["type"] == "original" else 1)
            if row["type"] == "original" and confirmed:
                value["original"] = True
        flush_candidates()

    def check_local_results(self, job, control):
        with self.store.session():
            try:
                return self._check_local_results(job, control)
            finally:
                if control:
                    control.verification_records = None

    def _check_local_results(self, job, control):
        """Finish local verification before any download workers are started."""
        control = control or Control()
        job_id = job["id"]
        with self.store.db() as db:
            total = db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND kind='file' AND status!='filtered'", (job_id,)).fetchone()[0]
        checked = found = 0
        originals = previews = 0
        cursor = ""
        updates, remove_previews = [], []
        def flush():
            if not updates and not remove_previews:
                return
            with self.store.db() as db:
                db.executemany("UPDATE entries SET status=?,priority=?,error='',original_status=?,preview_status=?,fallback=0 WHERE job=? AND path=?", updates)
                db.executemany("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", remove_previews)
            updates.clear()
            remove_previews.clear()
        control.event("cache", "Проверка папки перед дозагрузкой")
        while True:
            control.check()
            with self.store.db() as db:
                rows = db.execute("SELECT * FROM entries WHERE job=? AND kind='file' AND status!='filtered' AND path>? ORDER BY path LIMIT 200", (job_id, cursor)).fetchall()
            if not rows:
                break
            with self.store.db() as db:
                artifacts = db.execute('SELECT a.*,v.signature FROM artifacts a LEFT JOIN verification_cache v ON v.job=a.job AND v.local=a.local WHERE a.job=? AND a.path>=? AND a.path<=?', (job_id,rows[0]['path'],rows[-1]['path'])).fetchall()
            control.verification_records = {(row['path'],row['type']):dict(row) for row in artifacts}
            for row in rows:
                control.check()
                entry = dict(row)
                control.progress(entry["path"], checked, total, "Проверка скачанной папки", unit="objects")
                meta = json.loads(entry["meta"])
                original = preview = False
                try:
                    original = self.reusable(job, entry, "original", meta, control)
                    preview = not original and preview_type(meta) and self.reusable(job, entry, "preview", meta, control)
                    if (original or preview) and job["settings"].get("operation") != "missing":
                        # A restart resumes a saved inventory. Before skipping
                        # its verified copies, check whether the source changed.
                        try:
                            current = self.api.metadata(entry["path"], job["settings"], control.check, limit=1)
                            if current.get("type") != "file" or fingerprint(current) != fingerprint(meta):
                                original = preview = False
                                with self.store.db() as db:
                                    db.execute("DELETE FROM artifacts WHERE job=? AND path=?", (job_id, entry["path"]))
                        except APIError:
                            # Let the normal per-file path record a readable
                            # error / retry; do not silently accept stale data.
                            original = preview = False
                except (OSError, ValueError):
                    # An unreadable local file must not prevent the remaining folder check.
                    with self.store.db() as db:
                        db.execute("DELETE FROM artifacts WHERE job=? AND path=?", (job_id, entry["path"]))
                if original:
                    desired = ('existing',2,'existing_verified','not_needed')
                    if (entry['path'],'preview') in control.verification_records:
                        remove_previews.append((job_id,entry['path']))
                elif preview and job['settings'].get('missing_policy','originals') == 'any_result':
                    desired = ('existing',1,'not_requested','existing_verified')
                else:
                    desired = ('pending',int(bool(preview)),'not_requested','existing_verified' if preview else 'not_requested')
                previous = tuple(entry[key] for key in ('status','priority','original_status','preview_status'))
                if previous != desired or entry['error'] or entry['fallback']:
                    updates.append((*desired,job_id,entry['path']))
                if not original:
                    # Publish invalidations before checking a slow / unavailable
                    # next file. Verified result records remain durable per file.
                    flush()
                checked += 1
                found += bool(original or preview)
                originals += bool(original)
                previews += bool(preview)
                with control.lock:
                    control.active.clear()
                    control.verification = {'checked':checked,'total':total,'originals':originals,'previews':previews,'missing':checked-found,'complete':checked==total}
                control.progress(entry["path"], checked, total, "Проверка скачанной папки", unit="objects")
            cursor = rows[-1]["path"]
            flush()
        control.event("cache", f"Папка проверена: {checked} файлов, уже есть результат у {found}. Сначала будут скачаны файлы без результата.")

    def remember_file(self, job, relative, sha):
        signature = file_signature(safe_join(job["root"], relative), sha)
        if signature[-1] is None:
            return
        signature = json.dumps(signature)
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO verification_cache VALUES(?,?,?)", (job["id"], relative, signature))

    def partials_digest(self, filename, size, control):
        from .partials import digest_prefix
        sha, md5 = digest_prefix(filename, size, control.check)
        return sha.hexdigest(), md5.hexdigest()

    def reusable(self, job, entry, kind, meta, control):
        records = getattr(control,'verification_records',None)
        if records is not None:
            artifact = records.get((entry['path'],kind))
        else:
            with self.store.db() as db:
                artifact = db.execute("SELECT a.*,v.signature FROM artifacts a LEFT JOIN verification_cache v ON v.job=a.job AND v.local=a.local WHERE a.job=? AND a.path=? AND a.type=?", (job["id"], entry["path"], kind)).fetchone()
        if not artifact:
            if job["settings"].get("operation") != "missing" or kind != "original" or not (meta.get("md5") or meta.get("sha256")):
                return False
            filename = safe_join(job["root"], entry["local"])
            if not os.path.isfile(filename) or meta.get("size") is not None and os.path.getsize(filename) != meta["size"]:
                return False
            before = file_signature(filename, "")
            sha, md5 = hashlib.sha256(), hashlib.md5()
            with open(filename, "rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    control.check()
                    sha.update(chunk)
                    md5.update(chunk)
            if meta.get("sha256") and sha.hexdigest() != meta["sha256"].lower() or meta.get("md5") and md5.hexdigest() != meta["md5"].lower():
                return False
            if before != file_signature(filename, ""):
                return False
            with self.store.db() as db:
                db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job["id"], entry["path"], kind, entry["local"], os.path.getsize(filename), sha.hexdigest(), fingerprint(meta), None))
            self.remember_file(job, entry["local"], sha.hexdigest())
            return True
        artifact = dict(artifact)
        try:
            filename = safe_join(job["root"], artifact["local"])
            valid = artifact["fingerprint"] == fingerprint(meta) and (kind != "preview" or (artifact["preview_size"] == job["settings"]["preview_size"] or job["settings"].get("missing_policy") == "any_result") and artifact["size"] <= PREVIEW_MAX_BYTES)
            if kind == "original" and meta.get("sha256"):
                valid = valid and artifact["sha256"].lower() == meta["sha256"].lower()
            import stat
            signature = file_signature(filename, artifact['sha256']) if valid else None
            valid = valid and stat.S_ISREG(os.stat(filename).st_mode) and signature[2] == artifact['size']
            if valid:
                encoded = json.dumps(signature)
                cached = signature[-1] is not None and artifact.get('signature') == encoded
                hashed = 0
                def hash_checkpoint():
                    nonlocal hashed
                    control.check()
                    hashed = min(artifact['size'],hashed+1024*1024)
                    control.progress(entry['path'],hashed,artifact['size'],'Проверяем содержимое файла на ПК')
                if not cached and kind == "original" and meta.get("md5") and not meta.get("sha256"):
                    from .partials import digest_prefix
                    sha, md5 = digest_prefix(filename, artifact['size'], hash_checkpoint)
                    valid = sha.hexdigest() == artifact["sha256"] and md5.hexdigest().lower() == meta["md5"].lower()
                else:
                    valid = cached or file_hash(filename, hash_checkpoint) == artifact["sha256"]
                if valid and not cached and kind == "preview":
                    with Image.open(filename) as image:
                        if image.width * image.height > 16_000_000:
                            valid = False
                        else:
                            image.verify()
                    if valid:
                        with Image.open(filename) as image:
                            image.load()
                valid = valid and signature == file_signature(filename, artifact["sha256"])
                if valid and not cached and signature[-1] is not None:
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO verification_cache VALUES(?,?,?)", (job["id"], artifact["local"], encoded))
        except (OSError, ValueError):
            valid = False
        if valid:
            return True
        with self.store.db() as db:
            db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type=?", (job["id"], entry["path"], kind))
        return False

    def process(self, job_id, entry, control):
        with self.store.session():
            return self._process(job_id, entry, control)

    def _process(self, job_id, entry, control):
        job = self.store.job(job_id)
        path = entry["path"]
        mode = job["settings"]["mode"]
        if job["settings"].get("operation") == "missing" or job["generation"] > 0:
            mode = "fallback"
        errors = []
        other_errors = []
        completed, reused = [], []
        original_status = preview_status = "not_requested"
        fallback = False
        finalized = False
        try:
            control.check()
            self.store.update_entry(job_id, path, error="", original_attempts=0, preview_attempts=0, original_status="not_requested", preview_status="not_requested", fallback=0)
            control.progress(path, 0, None, "Проверка метаданных")
            meta = self.api.metadata(path, job["settings"], control.check, limit=1)
            validate_resource(meta)
            if meta["type"] != "file":
                raise APIError("Исходный файл теперь является папкой")
            with self.store.db() as db:
                safe = {k: meta[k] for k in ("path", "name", "type", "size", "md5", "sha256", "modified", "resource_id", "mime_type", "media_type") if k in meta}
                safe["file_type"] = file_type(meta)
                safe["has_preview"] = preview_type(meta) and bool(meta.get("preview"))
                db.execute("UPDATE entries SET meta=? WHERE job=? AND path=?", (json.dumps(safe, ensure_ascii=False), job_id, path))
                db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND fingerprint!=?", (job_id, path, fingerprint(meta)))
                if not preview_type(meta):
                    db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", (job_id, path))
                if not selected_type(meta, job["settings"]):
                    db.execute("DELETE FROM artifacts WHERE job=? AND path=?", (job_id, path))
                    db.execute("UPDATE entries SET status='filtered',error='' WHERE job=? AND path=?", (job_id, path))
                    return
            if not preview_type(meta):
                mode = "originals"
            kinds = ["preview"] if mode == "previews" else ["original"] if mode == "originals" else ["original", "preview"]
            for kind in kinds:
                control.check()
                if kind == "preview" and "original" in completed and (mode == "fallback" or "original" in reused):
                    preview_status = "not_needed"
                    with self.store.db() as db:
                        db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", (job_id, path))
                    break
                if kind == "preview" and mode == "fallback":
                    fallback = True
                try:
                    was_reused = self.reusable(job, entry, kind, meta, control)
                    if was_reused:
                        reused.append(kind)
                        outcome = "existing_verified"
                    else:
                        self.transfer(job, entry, kind, meta, control)
                        outcome = "downloaded_verified"
                    completed.append(kind)
                    label = "Оригинал" if kind == "original" else "Превью"
                    result = (" уже скачан, проверка пройдена" if kind == "original" else " уже скачано, проверка пройдена") if was_reused else (" скачан и проверен" if kind == "original" else " скачано и проверено")
                    control.event("cache" if was_reused else kind, label + result + (" · сохранено вместо недоступного оригинала" if fallback else ""), path, "success")
                except Suspended:
                    raise
                except Exception as exc:
                    outcome = "unavailable" if isinstance(exc, APIError) and exc.status == 404 and kind == "preview" else "failed"
                    errors.append(("Оригинал: " if kind == "original" else "Превью: ") + safe_error(exc))
                    if isinstance(exc, (PathError, sqlite3.Error)) or isinstance(exc, OSError) and not isinstance(exc, requests.RequestException):
                        other_errors.append(safe_error(exc))
                    control.event(kind, ("Оригинал: " if kind == "original" else "Превью: ") + safe_error(exc), path, "warning")
                if kind == "original":
                    original_status = outcome
                    self.store.update_entry(job_id, path, original_status=outcome)
                else:
                    preview_status = outcome
            if mode == "previews" and preview_status == "unavailable":
                status = "no_preview"
            elif mode == "both" and len(completed) == 1 and preview_status != "not_needed":
                status = "partial"
            elif not completed:
                status = "error"
            else:
                status = "existing" if len(reused) == len(completed) else "done"
            if not completed or other_errors:
                control.event("errors", "; ".join(errors if not completed else other_errors), path, "error")
            self.store.update_entry(job_id, path, status=status, error="; ".join(errors if not completed else other_errors), original_status=original_status, preview_status=preview_status, fallback=int(fallback))
            finalized = True
        except Suspended:
            self.store.update_entry(job_id, path, status="pending", original_status=original_status, preview_status=preview_status)
            control.stop()
            raise
        except APIError as exc:
            if control.adaptive:
                control.adaptive.failure(exc.status)
            if exc.status == 401:
                control.stop()
                raise Suspended(str(exc))
            # A failed source lookup says nothing about an already saved copy.
            # Only a successful metadata comparison / local verification may
            # invalidate it. Keep it in reports and archives during outages.
            self.store.update_entry(job_id, path, status="error", error=safe_error(exc), original_status=original_status, preview_status=preview_status)
            finalized = True
            control.event("errors", "Ошибка обработки файла: " + safe_error(exc), path, "error")
        except Exception as exc:
            self.store.update_entry(job_id, path, status="error", error=safe_error(exc), original_status=original_status, preview_status=preview_status)
            finalized = True
            control.event("errors", "Ошибка обработки файла: " + safe_error(exc), path, "error")
        finally:
            with control.lock:
                control.active.pop(path, None)
                if finalized:
                    control.remaining_time.result(bool(completed), reused=bool(completed) and len(reused) == len(completed) and not errors, fallback=fallback)

    def transfer(self, job, entry, kind, meta, control):
        import re
        settings = job["settings"]
        tries = attempt_count(settings)
        delay = 0
        stamp = fingerprint(meta) + ":" + kind + (":" + settings["preview_size"] if kind == "preview" else "")
        persistent = settings.get("resume_parts", True)
        attempt = 0
        while attempt < tries:
            control.check()
            control.network.wait(control.check)
            response = session = None
            pending_bytes = 0
            if delay:
                control.progress(entry["path"], 0, meta.get("size") if kind == "original" else None, f"Ожидание перед попыткой {attempt + 1}/{tries}")
                self.api.wait(delay, control.check)
            row, temp, sha, md5 = self.partials.prepare(job, entry["path"], kind, stamp, control.check)
            size = offset = row["size"]
            etag, total = row["etag"], row["total"]
            can_resume = persistent and (kind == "original" and (meta.get("sha256") or meta.get("md5")) or bool(etag))
            if size and not can_resume:
                with open(temp, "wb"):
                    pass
                size = offset = 0
                sha, md5 = hashlib.sha256(), hashlib.md5()
            with control.lock:
                control.temporary.add(temp)
                control.temporary_sizes[temp] = size
            keep = persistent and kind == "original"
            deadline = Deadline(settings.get("total_timeout", settings["timeout"]), control.check, control.active_clock, lambda: control.stopped or not self.api.credentials.connected, idle_seconds=settings["timeout"])
            try:
                with deadline:
                    deadline.check()
                    self.store.update_entry(job["id"], entry["path"], **{kind + "_attempts": attempt + 1})
                    label = "Оригинал" if kind == "original" else "Превью"
                    control.event(kind, f"{label}: попытка {attempt + 1}/{tries}", entry["path"])
                    complete_part = bool(kind == "original" and size and meta.get("size") == size and total in (None, size) and (meta.get("sha256") or meta.get("md5")))
                    if not complete_part:
                        media_settings = {**settings, "_single_attempt": True, "_deadline": deadline}
                        if offset:
                            media_settings.update(_range_offset=offset, _range_etag=etag)
                        if kind == "preview" and attempt == 0:
                            media_settings["_preview_meta"] = meta
                        control.progress(entry["path"], size, meta.get("size") if kind == "original" else total, f"{label}: попытка {attempt + 1}")
                        response, session = self.api.media(entry["path"], kind, media_settings, deadline.check)
                        raw_length = response.headers.get("Content-Length")
                        expected = int(raw_length) if raw_length and raw_length.isdigit() else None
                        new_etag = strong_etag(response.headers.get("ETag", ""))
                        if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                            keep = False
                            raise APIError("Сжатый ответ нельзя безопасно продолжить")
                        if response.status_code == 206:
                            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                            if not offset or not match:
                                keep = False
                                raise APIError("Некорректный ответ продолжения файла")
                            begin, finish, full = map(int, match.groups())
                            if begin != offset or finish < begin or finish >= full or total is not None and full != total or kind == "original" and meta.get("size") is not None and full != meta["size"] or etag and new_etag != etag or expected is not None and expected != finish - begin + 1:
                                keep = False
                                raise APIError("Сервер вернул несовместимую часть файла")
                            total = full
                            control.event(kind, f"Продолжение с байта {offset}", entry["path"])
                        elif response.status_code == 200:
                            if offset:
                                control.event(kind, "Сервер не подтвердил продолжение; загрузка начата заново", entry["path"], "warning")
                            offset = size = 0
                            sha, md5 = hashlib.sha256(), hashlib.md5()
                            total = expected
                        else:
                            raise APIError(f"Ошибка загрузки HTTP {response.status_code}", response.status_code)
                        if kind == "original" and meta.get("size") is not None and total is not None and total != meta["size"]:
                            keep = False
                            raise APIError("Размер ответа оригинала не совпадает с метаданными")
                        etag = new_etag
                        keep = persistent and (kind == "original" or bool(etag))
                        last_flush = last_checkpoint = time.monotonic()
                        checkpoint_size = size
                        with open(temp, "r+b") as stream:
                            stream.seek(offset)
                            stream.truncate(offset)
                            self.partials.save(job, entry["path"], kind, size, sha, etag, total)
                            chunk_size = min(65536, max(1024, int(settings.get("speed_limit_mib", 0) * 1024 * 1024 / 10))) if settings.get("speed_limit_mib", 0) else 256 * 1024
                            for chunk in response.iter_content(chunk_size):
                                deadline.check()
                                if not self.api.credentials.connected:
                                    raise Suspended("Диск отключён")
                                if not chunk:
                                    continue
                                deadline.touch()
                                if kind == "preview" and size + len(chunk) > PREVIEW_MAX_BYTES:
                                    keep = False
                                    raise APIError("Превью превышает допустимый объём")
                                with deadline.hold():
                                    control.rate_limiter.consume(len(chunk), control.check)
                                stream.write(chunk)
                                sha.update(chunk)
                                md5.update(chunk)
                                size += len(chunk)
                                with control.lock:
                                    control.transferred += len(chunk)
                                    control.remaining_time.advance(kind, len(chunk))
                                    control.temporary_sizes[temp] = size
                                pending_bytes += len(chunk)
                                current = time.monotonic()
                                if pending_bytes >= 1024 * 1024 or current - last_flush >= 0.25:
                                    with self.store.db() as db:
                                        db.execute("UPDATE jobs SET downloaded=downloaded+? WHERE id=?", (pending_bytes, job["id"]))
                                    pending_bytes, last_flush = 0, current
                                if size - checkpoint_size >= 4 * 1024 * 1024 or current - last_checkpoint >= 1:
                                    stream.flush()
                                    os.fsync(stream.fileno())
                                    self.partials.save(job, entry["path"], kind, size, sha, etag, total)
                                    checkpoint_size, last_checkpoint = size, current
                                control.progress(entry["path"], size, meta.get("size") if kind == "original" and meta.get("size") is not None else total, "Загрузка " + ("оригинала" if kind == "original" else "превью"), kind=kind, source_size=meta.get("size"))
                            deadline.received()
                            stream.flush()
                            os.fsync(stream.fileno())
                        self.partials.save(job, entry["path"], kind, size, sha, etag, total)
                        deadline.check()
                        if expected is not None and size - offset != expected or total is not None and size != total:
                            raise APIError("Получен неполный ответ загрузки")
                    deadline.received()
                    if kind == "original":
                        if meta.get("size") is not None and size != meta["size"]:
                            keep = False
                            raise APIError("Размер оригинала не совпадает с метаданными")
                        if meta.get("sha256") and sha.hexdigest().lower() != meta["sha256"].lower():
                            keep = False
                            raise APIError("SHA-256 оригинала не совпадает")
                        if meta.get("md5") and md5.hexdigest().lower() != meta["md5"].lower():
                            keep = False
                            raise APIError("MD5 оригинала не совпадает")
                        proposed = entry["local"]
                    else:
                        keep = False
                        with Image.open(temp) as image:
                            fmt = image.format
                            if image.width * image.height > 16_000_000:
                                keep = False
                                raise APIError("Превью превышает безопасный размер изображения")
                            image.verify()
                        with Image.open(temp) as image:
                            image.load()
                        extensions = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif", "AVIF": "avif"}
                        if fmt not in extensions:
                            keep = False
                            raise APIError("Неизвестный формат изображения превью")
                        proposed = entry["local"] + ".preview." + extensions[fmt]
                    # Synthetic names cannot collide with a real disk:/ path.
                    identity = entry["path"] if kind == "original" else "@preview:" + entry["path"]
                    relative = self.allocate(job["id"], identity, proposed)
                    base_relative = relative
                    target = safe_join(job["root"], relative)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    counter = 0
                    while os.path.exists(target):
                        counter += 1
                        parent, _, name = base_relative.rpartition("/")
                        name = suffix(name, digest(fingerprint(meta) + kind + str(counter)))
                        version_identity = "@version:" + kind + ":" + entry['path'] + ":" + fingerprint(meta) + ":" + str(counter)
                        relative = self.allocate(job["id"], version_identity, (parent + "/" if parent else "") + name)
                        target = safe_join(job["root"], relative)
                    deadline.check()
                    with self.store.db() as db:
                        db.execute('INSERT OR REPLACE INTO pending_publications VALUES(?,?,?,?,?,?,?,?,?)', (job['id'], entry['path'], kind, Path(temp).name, relative, size, sha.hexdigest(), fingerprint(meta), settings['preview_size'] if kind == 'preview' else None))
                    publish(temp, target)
                    self.partials.discard(job, entry["path"], kind, remove=False)
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job["id"], entry["path"], kind, relative, size, sha.hexdigest(), fingerprint(meta), settings["preview_size"] if kind == "preview" else None))
                        db.execute('DELETE FROM pending_publications WHERE job=? AND path=? AND type=?', (job['id'], entry['path'], kind))
                    if control.adaptive:
                        control.adaptive.success()
                    self.remember_file(job, relative, sha.hexdigest())
                    return
            except Suspended:
                raise
            except Exception as exc:
                error = exc
                if deadline.pause_aborted.is_set() or isinstance(exc, PausedRequest):
                    error = PausedRequest()
                else:
                    with control.lock:
                        control.remaining_time.retry(kind)
                if not isinstance(error, PausedRequest) and deadline.network_aborted.is_set():
                    error = NetworkError(offline=True)
                elif not isinstance(error, PausedRequest) and deadline.expired.is_set():
                    error_type = NetworkError if isinstance(exc, (requests.RequestException, NetworkError)) else APIError
                    error = error_type(deadline.timeout_message())
                if control.adaptive and not isinstance(error, OSError):
                    control.adaptive.failure(error.status if isinstance(error, APIError) else None)
                if not isinstance(error, PausedRequest):
                    control.event(kind, f"{'Оригинал' if kind == 'original' else 'Превью'} · попытка {attempt + 1}/{tries}: {safe_error(error)}", entry["path"], "warning")
                if isinstance(error, APIError) and error.status == 416:
                    keep = False
            finally:
                if response is not None:
                    self.api.close(response, session)
                if pending_bytes:
                    with self.store.db() as db:
                        db.execute("UPDATE jobs SET downloaded=downloaded+? WHERE id=?", (pending_bytes, job["id"]))
                if os.path.exists(temp):
                    try:
                        if keep and size:
                            with open(temp, "r+b") as saved:
                                saved.truncate(size)
                                saved.flush()
                                os.fsync(saved.fileno())
                            self.partials.save(job, entry["path"], kind, size, sha, etag, total)
                        else:
                            os.unlink(temp)
                            self.partials.discard(job, entry["path"], kind, remove=False)
                    except OSError:
                        pass
                with control.lock:
                    control.temporary.discard(temp)
                    control.temporary_sizes.pop(temp, None)
            if isinstance(error, PausedRequest):
                control.check()
                delay = 0
                continue
            def connected_check():
                control.check()
                if not self.api.credentials.connected:
                    raise Suspended("Диск отключён")
            if control.network.recover(error, connected_check):
                delay = 0
                continue
            if isinstance(error, OSError) and not isinstance(error, requests.RequestException) or isinstance(error, APIError) and error.status == 404 and kind == "preview" or attempt == tries - 1:
                raise error
            delay = error.retry_after if isinstance(error, APIError) and error.retry_after is not None else retry_delay(None, attempt)
            attempt += 1

    def pack(self, job_id, control, manual=False):
        job = self.store.job(job_id)
        summary = self.store.summary(job_id)
        final_phase = "stopped" if manual and (not job["scan_complete"] or summary["cancelled"] or summary["processed"] < summary["files"] or job["phase"] in ("stopped", "interrupted", "failed")) else ("completed_with_errors" if summary["errors"] or summary["scan_errors"] else "completed")
        if control.selected_paths and (not job["scan_complete"] or summary["cancelled"] or summary["processed"] < summary["files"]):
            final_phase = "stopped"
        self.store.update_job(job_id, ended=job["ended"] or now())
        self.phase(job_id, control, "preparing")
        control.progress(job["settings"]["paths"][0], 0, None, "Создание отчётов", unit="objects")
        control.check()
        with REPORT_LOCK:
            reports = write_reports(self.store, job_id, control.check, final_phase=final_phase)
            reports.append(write_changes(self.store, job_id, control.check))
            with control.lock:
                control.active.clear()
            if not manual and not job["settings"].get("create_zip", True):
                self.store.update_job(job_id, phase=final_phase, generation=job["generation"] + 1)
                control.event("archive", "Файлы и отчёты сохранены; создание ZIP выключено", level="success")
                return
            total = summary["saved_bytes"] + sum(os.path.getsize(safe_join(job["root"], r)) for r in reports)
            generation = job["generation"] + 1
            archive_rel = reserve_output(self.store, job, f"result-{generation}.zip")
            archive = safe_join(job["root"], archive_rel)
            temp = temporary_file(job["root"], self.store, 'zip')
            with control.lock:
                control.temporary.add(temp)
            self.store.update_job(job_id, zip_total=total, zip_done=0, zip_path=None, zip_bytes=0)
            self.phase(job_id, control, "packing")
        pending_zip, last_flush = 0, time.monotonic()
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                for row in self.store.rows("SELECT local FROM entries WHERE job=? AND kind='dir' AND local!='' ORDER BY local", (job_id,)):
                    control.check()
                    relative = row["local"]
                    if os.path.isdir(safe_join(job["root"], relative)):
                        output.writestr(relative + "/", b"")
                report_folder = job["service"] + "/reports"
                output.writestr(report_folder + "/", b"")
                artifacts = self.store.rows("SELECT a.local,a.size,a.sha256 FROM artifacts a JOIN entries e ON e.job=a.job AND e.path=a.path WHERE a.job=? ORDER BY a.local", (job_id,))
                files = ((r["local"], r["size"], r["sha256"], r["local"]) for r in artifacts)
                import itertools
                report_files = ((r, os.path.getsize(safe_join(job["root"], r)), None, report_folder + ("/changes.html" if r == reports[-1] else "/report." + r.rsplit(".", 1)[1])) for r in reports)
                for relative, expected_size, expected_sha, archive_name in itertools.chain(files, report_files):
                    control.check()
                    filename = safe_join(job["root"], relative)
                    info = zipfile.ZipInfo(archive_name, datetime.now().timetuple()[:6])
                    info.compress_type = zipfile.ZIP_STORED
                    info.file_size = expected_size
                    done, sha = 0, hashlib.sha256()
                    with open(filename, "rb") as source, output.open(info, "w", force_zip64=True) as target:
                        while chunk := source.read(1024 * 1024):
                            control.check()
                            target.write(chunk)
                            with control.lock:
                                control.temporary_sizes[temp] = output.fp.tell()
                            sha.update(chunk)
                            done += len(chunk)
                            control.progress(relative, done, expected_size, "Создание ZIP")
                            pending_zip += len(chunk)
                            if pending_zip >= 4 * 1024 * 1024 or time.monotonic() - last_flush >= 0.25:
                                with self.store.db() as db:
                                    db.execute("UPDATE jobs SET zip_done=zip_done+? WHERE id=?", (pending_zip, job_id))
                                pending_zip, last_flush = 0, time.monotonic()
                    if done != expected_size or expected_sha and sha.hexdigest() != expected_sha:
                        raise APIError("Локальный результат изменён: архив не подтверждён")
                    with control.lock:
                        control.active.pop(relative, None)
            publish(temp, archive)
            self.store.update_job(job_id, phase=final_phase, zip_path=archive_rel, zip_bytes=os.path.getsize(archive), generation=generation)
            control.event("archive", "Отчёты и ZIP сохранены: " + archive_rel, level="success")
        finally:
            if pending_zip:
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET zip_done=zip_done+? WHERE id=?", (pending_zip, job_id))
            if os.path.exists(temp):
                os.unlink(temp)
            with control.lock:
                control.temporary.discard(temp)
                control.temporary_sizes.pop(temp, None)
