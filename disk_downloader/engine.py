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

from .api import APIError, Deadline, SIZES, Suspended, attempt_count, retry_delay
from .import_state import find_import, inherit, save_import
from .file_types import FILE_TYPES, file_type, preview_type, selected_type
from .estimation import RemainingTime
from .paths import PathError, component, digest, native, normalize_selection, parent_path, publish, remote_path, reserve_output, safe_join, suffix, temporary_file
from .reports import write_reports
from .journal import Journal, PHASES
from .adaptive import AdaptiveWorkers
from .partials import Partials, strong_etag


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
    if isinstance(exc, (APIError, PathError, Suspended)):
        return str(exc)
    if isinstance(exc, OSError):
        if exc.errno == 28 or getattr(exc, "winerror", None) == 112:
            return "Недостаточно места на диске"
        return f"Ошибка файловой системы (код {exc.errno or getattr(exc, 'winerror', 0)})"
    if isinstance(exc, requests.RequestException):
        return "Сетевая ошибка или таймаут"
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
        self.remaining_time = RemainingTime(self.active_clock)
        self.journal = None
        self.space_check = None
        self.space_lock = threading.Lock()
        self.next_space_check = 0
        self.pause_reason = ""
        self.adaptive = None
        self.selected_paths = None

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

    def progress(self, path, done, size, action, **details):
        with self.lock:
            self.active[path] = {"path": path, "done": done, "size": size, "action": action, **details}

    def snapshot(self):
        with self.lock:
            elapsed = (self.finished or self.pause_started or time.monotonic()) - self.started - self.elapsed_before
            return {"active": list(self.active.values()), "speed": self.transferred / max(0.1, elapsed)}

    def active_clock(self):
        with self.condition:
            current = time.monotonic()
            return current - self.elapsed_before - (current - self.pause_started if self.pause_started is not None else 0)


class Engine:
    def __init__(self, store, api):
        self.store = store
        self.api = api
        self.lock = threading.RLock()
        self.controls = {}
        self.threads = {}
        self.journal_interval = 300
        self.partials = Partials(store)

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
        for name, default, low, high in (("workers", 8, 1, 8), ("attempts", attempt_count(data), 1, 9), ("timeout", 20, 5, 180)):
            value = data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name}: допустимый диапазон {low}–{high}")
            values[name] = value
        operation = data.get("operation", "new")
        if operation not in ("new", "missing"):
            raise ValueError("Некорректный способ загрузки")
        values["retries"] = values["attempts"] - 1
        for name, default in (("create_zip", True), ("auto_pause", True), ("resume_parts", True), ("adaptive_workers", True)):
            value = data.get(name, default)
            if not isinstance(value, bool):
                raise ValueError("Некорректная настройка " + name)
            values[name] = value
        fullness = data.get("max_disk_percent", 95)
        if isinstance(fullness, bool) or not isinstance(fullness, (int, float)) or not 1 <= fullness <= 99.9:
            raise ValueError("Порог заполнения диска: от 1 до 99,9%")
        values["max_disk_percent"] = fullness
        return {"mode": mode, "preview_size": size, "reports": sorted(set(reports)), "file_types": sorted(set(file_types)),
                "paths": normalize_selection(paths, data.get("whole_disk", False)), "whole_disk": data.get("whole_disk", False),
                "destination": destination, "operation": operation, **values}

    def create(self, data):
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
        if previous:
            inherit(self.store, job_id, previous)
        self.launch(job_id, "scan")
        return job_id

    def launch(self, job_id, operation="download", paths=None):
        with self.lock:
            if any(t.is_alive() for t in self.threads.values()):
                raise ValueError("Дождитесь завершения текущей операции или остановите её")
            if operation != "pack" and not self.api.credentials.connected:
                raise ValueError("Подключите Диск перед продолжением")
            control = Control()
            control.journal = Journal(self, job_id, self.journal_interval)
            job = self.store.job(job_id)
            control.adaptive = AdaptiveWorkers(job["settings"]["workers"], job["settings"].get("adaptive_workers", True), control.active_clock)
            control.space_check = lambda: self.check_space(job_id, control)
            control.selected_paths = tuple(paths) if paths else None
            self.controls[job_id] = control
            thread = threading.Thread(target=self.run, args=(job_id, operation, control), daemon=True)
            self.threads[job_id] = thread
            thread.start()

    def action(self, job_id, action, limits=None):
        job = self.store.job(job_id)
        with self.lock:
            live = self.threads.get(job_id)
            control = self.controls.get(job_id)
            if action == "pause":
                if not live or not live.is_alive() or job["phase"] not in ("scanning", "downloading", "packing"):
                    raise ValueError("Нет активной операции для паузы")
                control.pause()
                control.pause_reason = ""
                self.store.update_job(job_id, phase="paused", pause_reason="")
                control.event("system", "Задание поставлено на паузу")
                return
            if action == "stop":
                if live and live.is_alive():
                    self.store.update_job(job_id, phase="stopping")
                    control.event("system", "Запрошена остановка задания", level="warning")
                    control.stop()
                else:
                    self.store.update_job(job_id, phase="stopped", ended=now())
                    with self.store.db() as db:
                        db.execute("UPDATE entries SET status='cancelled' WHERE job=? AND kind='file' AND status IN ('pending','running')", (job_id,))
                    if control and control.journal:
                        control.event("system", "Задание остановлено", level="warning")
                        control.journal.flush()
                return
            if action == "resume" and live and live.is_alive() and control.paused:
                if limits and any(limits.get(k, job["settings"].get(k)) != job["settings"].get(k, attempt_count(job["settings"]) if k == "attempts" else None) for k in ("workers", "attempts", "timeout")):
                    raise ValueError("Для изменения ограничений остановите задание и продолжите с новыми параметрами")
                if not self.api.credentials.connected and not job["scan_complete"]:
                    raise ValueError("Подключите Диск")
                if limits:
                    keys = ("create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts")
                    validated = self.validate_settings(job["settings"] | {key: limits[key] for key in keys if key in limits})
                    job["settings"].update({key: validated[key] for key in keys})
                    self.store.update_job(job_id, settings=json.dumps(job["settings"], ensure_ascii=False))
                    control.adaptive.configure(validated["adaptive_workers"])
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
                if limits:
                    keys = ("workers", "attempts", "timeout", "create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts")
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
                        db.execute("UPDATE entries SET status='pending', error='' WHERE job=? AND kind='file' AND (status IN ('error','partial','no_preview','cancelled') OR fallback=1)", (job_id,))
                        db.execute("UPDATE entries SET scanned=0 WHERE job=? AND kind='dir' AND path IN (SELECT path FROM scan_errors WHERE job=?)", (job_id, job_id))
                        has_scan_errors = db.execute("SELECT COUNT(*) FROM scan_errors WHERE job=?", (job_id,)).fetchone()[0]
                        if has_scan_errors:
                            db.execute("UPDATE jobs SET scan_complete=0 WHERE id=?", (job_id,))
                    else:
                        db.execute("UPDATE entries SET status='pending' WHERE job=? AND kind='file'", (job_id,))
                self.launch(job_id, "download", paths=paths if action == "retry_selected" else None)
                return
            if action == "pack":
                self.launch(job_id, "pack")
                return
            raise ValueError("Неизвестная команда")

    def disconnect(self):
        with self.lock:
            for control in self.controls.values():
                control.stop()
        self.api.credentials.clear()

    def status(self, job_id):
        live = bool(self.threads.get(job_id) and self.threads[job_id].is_alive())
        status = self.store.summary(job_id)
        control = self.controls.get(job_id)
        status.update(control.snapshot() if control else {"active": [], "speed": 0})
        status["live"] = live
        status["journal_saved"] = control.journal.saved if control and control.journal else None
        status["journal_error"] = control.journal.error if control and control.journal else ""
        status["pause_reason"] = control.pause_reason if control else status.get("pause_reason", "")
        status["workers_status"] = control.adaptive.snapshot() if control and control.adaptive else {"current": status["settings"]["workers"], "maximum": status["settings"]["workers"], "enabled": status["settings"].get("adaptive_workers", True), "reason": ""}
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
        status["eta_seconds"] = None
        status["eta_phase"] = None
        if live and control and status["phase"] in ("downloading", "packing"):
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
                    status["eta_seconds"] = control.remaining_time.estimate(phase, control.transferred, status["processed"], remaining_bytes, work["files"], unknown_files)
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
            self.store.update_job(job_id, error="", ended=None, pause_reason="")
            control.journal.start(operation)
            if operation == "pack":
                self.prepare_paths(job_id)
                job = self.store.job(job_id)
                self.store.update_job(job_id, ended=now(), phase="stopped" if not job["scan_complete"] else job["phase"])
                self.pack(job_id, control, manual=True)
                return
            if not self.store.job(job_id)["scan_complete"] and not control.selected_paths:
                self.phase(job_id, control, "scanning")
                self.scan(job_id, control)
            self.prepare_paths(job_id)
            save_import(self.store, job_id)
            if operation == "scan":
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
                save_import(self.store, job_id)
            except (OSError, sqlite3.Error, ValueError):
                pass
            with control.lock:
                control.active.clear()
                control.finished = time.monotonic()

    def scan(self, job_id, control):
        job = self.store.job(job_id)
        settings = job["settings"]
        for path in settings["paths"]:
            control.check()
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
            control.progress(path, offset, None, "Обход папки")
            try:
                meta = self.api.metadata(path, settings, control.check, offset=offset, limit=200)
                embedded = meta.get("_embedded")
                if not isinstance(embedded, dict) or not isinstance(embedded.get("items"), list):
                    raise APIError("API не вернул содержимое папки")
                items = embedded["items"]
                if not items and offset < embedded.get("total", offset):
                    raise APIError("Пустая страница до окончания списка API")
                with self.store.db() as db:
                    for item in items:
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

    def prepare_paths(self, job_id):
        job = self.store.job(job_id)
        for entry in self.store.rows("SELECT path,kind,meta FROM entries WHERE job=? ORDER BY path COLLATE BINARY", (job_id,)):
            relative = self.map_path(job_id, entry["path"])
            self.store.update_entry(job_id, entry["path"], local=relative)
            if entry["kind"] == "file":
                meta = json.loads(entry["meta"])
                with self.store.db() as db:
                    db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND fingerprint!=?", (job_id, entry["path"], fingerprint(meta)))
                    if not preview_type(meta):
                        db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", (job_id, entry["path"]))
            if entry["kind"] == "dir":
                os.makedirs(safe_join(job["root"], relative), exist_ok=True)

    def download(self, job_id, control):
        workers = self.store.job(job_id)["settings"]["workers"]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="disk-download") as pool:
            for stage in range(2):
                if stage:
                    self.prioritize(job_id, control)
                condition = "" if stage else " AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.job=entries.job AND a.path=entries.path)"
                parameters = (job_id,)
                if control.selected_paths:
                    condition += " AND path IN (" + ",".join("?" for _ in control.selected_paths) + ")"
                    parameters += control.selected_paths
                active = set()
                while True:
                    control.check()
                    while len(active) < (control.adaptive.limit() if control.adaptive else workers):
                        with self.store.db() as db:
                            entry = db.execute("SELECT * FROM entries WHERE job=? AND kind='file' AND status='pending'" + condition + " ORDER BY priority,path LIMIT 1", parameters).fetchone()
                            if entry:
                                db.execute("UPDATE entries SET status='running' WHERE job=? AND path=?", (job_id, entry["path"]))
                        if not entry:
                            break
                        active.add(pool.submit(self.process, job_id, dict(entry), control))
                    if not active:
                        break
                    done, active = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                    if control.adaptive:
                        control.adaptive.measure(control.transferred, len(active) + len(done))
                    for future in done:
                        future.result()

    def prioritize(self, job_id, control):
        job = self.store.job(job_id)
        with self.store.db() as db:
            db.execute("DELETE FROM verification_cache WHERE job=?", (job_id,))
            db.execute("UPDATE entries SET priority=0 WHERE job=? AND status='pending'", (job_id,))
        for entry in self.store.rows("SELECT e.* FROM entries e WHERE e.job=? AND e.kind='file' AND e.status='pending' AND EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path)", (job_id,)):
            control.check()
            control.progress(entry["path"], 0, None, "Проверка ранее скачанного")
            meta = json.loads(entry["meta"])
            original = self.reusable(job, entry, "original", meta, control)
            preview = self.reusable(job, entry, "preview", meta, control)
            self.store.update_entry(job_id, entry["path"], priority=2 if original else 1 if preview else 0)
            with control.lock:
                control.active.pop(entry["path"], None)

    def reusable(self, job, entry, kind, meta, control):
        with self.store.db() as db:
            artifact = db.execute("SELECT * FROM artifacts WHERE job=? AND path=? AND type=?", (job["id"], entry["path"], kind)).fetchone()
        if not artifact:
            if job["settings"].get("operation") != "missing" or kind != "original" or not (meta.get("md5") or meta.get("sha256")):
                return False
            filename = safe_join(job["root"], entry["local"])
            if not os.path.isfile(filename) or meta.get("size") is not None and os.path.getsize(filename) != meta["size"]:
                return False
            sha, md5 = hashlib.sha256(), hashlib.md5()
            with open(filename, "rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    control.check()
                    sha.update(chunk)
                    md5.update(chunk)
            if meta.get("sha256") and sha.hexdigest() != meta["sha256"] or meta.get("md5") and md5.hexdigest() != meta["md5"]:
                return False
            with self.store.db() as db:
                db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job["id"], entry["path"], kind, entry["local"], os.path.getsize(filename), sha.hexdigest(), fingerprint(meta), None))
            return True
        artifact = dict(artifact)
        try:
            filename = safe_join(job["root"], artifact["local"])
            valid = artifact["fingerprint"] == fingerprint(meta) and (kind != "preview" or artifact["preview_size"] == job["settings"]["preview_size"])
            valid = valid and os.path.isfile(filename) and os.path.getsize(filename) == artifact["size"]
            if valid:
                info = os.stat(filename)
                signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, artifact["sha256"])
                encoded = json.dumps(signature)
                with self.store.db() as db:
                    cached_row = db.execute("SELECT signature FROM verification_cache WHERE job=? AND local=?", (job["id"], artifact["local"])).fetchone()
                cached = cached_row is not None and cached_row[0] == encoded
                valid = cached or file_hash(filename, control.check) == artifact["sha256"]
                after = os.stat(filename)
                valid = valid and signature[:5] == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                if valid and not cached:
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO verification_cache VALUES(?,?,?)", (job["id"], artifact["local"], encoded))
        except OSError:
            valid = False
        if valid:
            return True
        with self.store.db() as db:
            db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type=?", (job["id"], entry["path"], kind))
        return False

    def process(self, job_id, entry, control):
        job = self.store.job(job_id)
        path = entry["path"]
        mode = job["settings"]["mode"]
        if job["settings"].get("operation") == "missing" or job["generation"] > 0:
            mode = "fallback"
        errors = []
        completed, reused = [], []
        original_status = preview_status = "not_requested"
        fallback = False
        try:
            control.check()
            self.store.update_entry(job_id, path, original_attempts=0, preview_attempts=0, original_status="not_requested", preview_status="not_requested", fallback=0)
            control.progress(path, 0, None, "Проверка метаданных")
            meta = self.api.metadata(path, job["settings"], control.check, limit=1)
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
                    control.event("errors", ("Оригинал: " if kind == "original" else "Превью: ") + safe_error(exc), path, "warning" if outcome == "unavailable" else "error")
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
            self.store.update_entry(job_id, path, status=status, error="; ".join(errors), original_status=original_status, preview_status=preview_status, fallback=int(fallback))
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
            with self.store.db() as db:
                db.execute("DELETE FROM artifacts WHERE job=? AND path=?", (job_id, path))
            self.store.update_entry(job_id, path, status="error", error=safe_error(exc), original_status=original_status, preview_status=preview_status)
            control.event("errors", "Ошибка обработки файла: " + safe_error(exc), path, "error")
        except Exception as exc:
            self.store.update_entry(job_id, path, status="error", error=safe_error(exc), original_status=original_status, preview_status=preview_status)
            control.event("errors", "Ошибка обработки файла: " + safe_error(exc), path, "error")
        finally:
            with control.lock:
                control.active.pop(path, None)

    def transfer(self, job, entry, kind, meta, control):
        import re
        settings = job["settings"]
        tries = attempt_count(settings)
        delay = 0
        stamp = fingerprint(meta) + ":" + kind + (":" + settings["preview_size"] if kind == "preview" else "")
        persistent = settings.get("resume_parts", True)
        for attempt in range(tries):
            control.check()
            response = session = None
            pending_bytes = 0
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
            deadline = Deadline(settings["timeout"], control.check, control.active_clock, lambda: control.stopped or not self.api.credentials.connected)
            try:
                with deadline:
                    deadline.check()
                    self.store.update_entry(job["id"], entry["path"], **{kind + "_attempts": attempt + 1})
                    label = "Оригинал" if kind == "original" else "Превью"
                    control.event(kind, f"{label}: попытка {attempt + 1}/{tries}", entry["path"])
                    if delay:
                        control.progress(entry["path"], size, total, f"Ожидание перед попыткой {attempt + 1}/{tries}")
                        self.api.wait(delay, deadline.check)
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
                            for chunk in response.iter_content(256 * 1024):
                                deadline.check()
                                if not self.api.credentials.connected:
                                    raise Suspended("Диск отключён")
                                if not chunk:
                                    continue
                                stream.write(chunk)
                                sha.update(chunk)
                                md5.update(chunk)
                                size += len(chunk)
                                with control.lock:
                                    control.transferred += len(chunk)
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
                            stream.flush()
                            os.fsync(stream.fileno())
                        self.partials.save(job, entry["path"], kind, size, sha, etag, total)
                        deadline.check()
                        if expected is not None and size - offset != expected or total is not None and size != total:
                            raise APIError("Получен неполный ответ загрузки")
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
                    identity = entry["path"] if kind == "original" else entry["path"] + "@preview"
                    relative = self.allocate(job["id"], identity, proposed)
                    target = safe_join(job["root"], relative)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    counter = 0
                    while os.path.exists(target):
                        counter += 1
                        parent, _, name = relative.rpartition("/")
                        name = suffix(name, digest(fingerprint(meta) + kind + str(counter)))
                        relative = self.allocate(job["id"], identity + "@version" + str(counter), (parent + "/" if parent else "") + name)
                        target = safe_join(job["root"], relative)
                    deadline.check()
                    publish(temp, target)
                    self.partials.discard(job, entry["path"], kind, remove=False)
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job["id"], entry["path"], kind, relative, size, sha.hexdigest(), fingerprint(meta), settings["preview_size"] if kind == "preview" else None))
                    if control.adaptive:
                        control.adaptive.success()
                    return
            except Suspended:
                raise
            except Exception as exc:
                error = APIError(f"Превышен лимит попытки: {settings['timeout']} с") if deadline.expired.is_set() else exc
                if control.adaptive and not isinstance(error, OSError):
                    control.adaptive.failure(error.status if isinstance(error, APIError) else None)
                control.event("errors", f"{'Оригинал' if kind == 'original' else 'Превью'} · попытка {attempt + 1}/{tries}: {safe_error(error)}", entry["path"], "warning" if attempt < tries - 1 else "error")
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
            if isinstance(error, OSError) or isinstance(error, APIError) and error.status == 404 and kind == "preview" or attempt == tries - 1:
                raise error
            delay = error.retry_after if isinstance(error, APIError) and error.retry_after is not None else retry_delay(None, attempt)

    def pack(self, job_id, control, manual=False):
        job = self.store.job(job_id)
        summary = self.store.summary(job_id)
        final_phase = "stopped" if manual and (not job["scan_complete"] or summary["cancelled"] or summary["processed"] < summary["files"] or job["phase"] in ("stopped", "interrupted", "failed")) else ("completed_with_errors" if summary["errors"] or summary["missing_previews"] or summary["scan_errors"] or summary["replacements"] else "completed")
        if control.selected_paths and (not job["scan_complete"] or summary["cancelled"] or summary["processed"] < summary["files"]):
            final_phase = "stopped"
        self.store.update_job(job_id, phase=final_phase, ended=job["ended"] or now())
        control.check()
        reports = write_reports(self.store, job_id)
        if not manual and not job["settings"].get("create_zip", True):
            self.store.update_job(job_id, phase=final_phase, generation=job["generation"] + 1)
            control.event("archive", "Файлы и отчёты сохранены; создание ZIP выключено", level="success")
            return
        total = summary["saved_bytes"] + sum(os.path.getsize(safe_join(job["root"], r)) for r in reports)
        generation = job["generation"] + 1
        archive_rel = reserve_output(self.store, job, f"result-{generation}.zip")
        archive = safe_join(job["root"], archive_rel)
        temp = temporary_file(job["root"])
        with control.lock:
            control.temporary.add(temp)
        self.store.update_job(job_id, zip_total=total, zip_done=0, zip_path=None, zip_bytes=0)
        self.phase(job_id, control, "packing")
        pending_zip, last_flush = 0, time.monotonic()
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                for row in self.store.rows("SELECT local FROM names WHERE job=? AND identity NOT LIKE '%@%' ORDER BY local", (job_id,)):
                    control.check()
                    relative = row["local"]
                    if os.path.isdir(safe_join(job["root"], relative)):
                        output.writestr(relative + "/", b"")
                report_folder = job["service"] + "/reports"
                output.writestr(report_folder + "/", b"")
                artifacts = self.store.rows("SELECT a.local,a.size,a.sha256 FROM artifacts a JOIN entries e ON e.job=a.job AND e.path=a.path WHERE a.job=? ORDER BY a.local", (job_id,))
                files = ((r["local"], r["size"], r["sha256"], r["local"]) for r in artifacts)
                import itertools
                report_files = ((r, os.path.getsize(safe_join(job["root"], r)), None, report_folder + "/report." + r.rsplit(".", 1)[1]) for r in reports)
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
