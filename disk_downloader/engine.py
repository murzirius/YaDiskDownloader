import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

import requests
from PIL import Image, UnidentifiedImageError
from .image_security import PREVIEW_FORMATS

from .api import APIError, Deadline, NetworkError, PausedRequest, PREVIEW_MAX_BYTES, SIZES, Suspended, attempt_count, retry_delay, validate_resource
from .backups import BackupTimeout
from .import_state import backup_import, find_import, inherit, read_import, save_import
from .file_types import FILE_TYPES, file_type, preview_type, selected_type
from .estimation import RemainingTime, VerificationTime
from .paths import file_signature, forget_temporary, PathError, component, digest, native, normalize_selection, parent_path, publish, remote_path, reserve_output, safe_join, suffix, temporary_file
from .reports import write_reports, REPORT_LOCK
from .journal import Journal, PHASES
from .partials import Partials, strong_etag
from .folder_usage import FolderUsage
from . import exclusions, rate_limit
from .inventory import compare, write_changes
from .network import NetworkWait
from .limits import MAX_WORKERS
from .status_cache import StatusCache
from .destination import DestinationGuard, StorageRetry
from .sqlite_support import error_details
from .space_budget import HARD_FREE_BYTES, RESUME_FREE_BYTES, may_finish


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
    if isinstance(exc, BackupTimeout):
        return str(exc)
    if isinstance(exc, UnidentifiedImageError):
        return 'Полученное превью не удалось прочитать как изображение. Яндекс.Диск мог вернуть неверные данные. Повторите загрузку файла.'
    if isinstance(exc, Image.DecompressionBombError):
        return 'Превью Яндекс.Диска превышает безопасный размер изображения. Этот вариант не сохранён; попробуйте загрузить оригинал.'
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
        detail = error_details(exc)
        return detail['message'] + ' Код: ' + detail['code'] + '. Не удаляйте историю или скачанные файлы. Если ошибка повторяется, нажмите «Диагностика» → «Посмотреть состав» → «Сохранить ZIP диагностики» до закрытия программы.'
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
        self.session_id = uuid.uuid4().hex
        self.session_started_at = now()
        self.session_saved_bytes = 0
        self.session_originals = 0
        self.session_previews = 0
        self.session_errors = 0
        self.timings = {}
        self.elapsed_before = 0
        self.pause_started = None
        self.finished = None
        self.temporary = set()
        self.temporary_sizes = {}
        self.journal = None
        self.network = NetworkWait(self.event, clock=self.active_clock)
        self.remaining_time = RemainingTime(self.statistics_clock)
        self.space_check = None
        self.space_lock = threading.RLock()
        self.space_draining = False
        self.finish_current = False
        self.floor_latched = False
        self.transfer_sizes = {}
        self.free_credit = None
        self.next_space_check = 0
        self.pause_reason = ""
        self.adaptive = None
        self.selected_paths = None
        self.rate_limiter = rate_limit.RateLimiter(0)
        self.verification_time = VerificationTime(self.active_clock)
        self.verification = None
        self.verification_cached = 0
        self.verification_hashed = 0
        self.duplicate_locks = [threading.RLock() for _ in range(64)]
        self.duplicate_cleanup_lock = threading.RLock()
        self.destination = None
        self.storage_check = None
        self.last_progress = self.started
        self.pause_acknowledged = False
        self.expected_waiters = 0
        self.pause_kind = ''
        self.pause_serial = 0
        self.storage_problem = False
        self.storage_problem_reason = ''
        self.storage_retry_thread = None
        self.storage_retry_due = None
        self.storage_retry_attempt = 0
        self.storage_retry_delays = (2, 4, 8, 16, 32, 60)

    def add_timing(self, category, seconds):
        with self.lock:
            self.timings[category] = self.timings.get(category, 0) + max(0, seconds)

    @contextmanager
    def measure(self, category):
        started = self.active_clock()
        try:
            yield
        finally:
            elapsed = max(0, self.active_clock() - started)
            with self.lock:
                self.timings[category] = self.timings.get(category, 0) + elapsed

    def event(self, group, message, path="", level="info"):
        if self.journal:
            self.journal.event(group, message, path, level)

    def requires_connection(self):
        return not getattr(self,'local_only',False) and getattr(self,'operation','download') not in ('pack','manifest','journal') and getattr(self,'phase','') != 'packing'

    def check(self):
        if getattr(self, 'policy', None):
            self.policy.check()
        if self.space_check and not self.paused:
            self.space_check()
        with self.condition:
            while self.paused and not self.stopped:
                self.pause_acknowledged = True
                self.condition.wait(0.2)
            if self.stopped:
                raise Suspended("Задание остановлено")

    def storage_error(self, error):
        if isinstance(error, OSError) and not isinstance(error, requests.RequestException) and self.storage_check:
            if self.storage_check():
                self.check()
                return True
        return False

    def before_write(self, size):
        # Charge writes between bounded OS probes so a fast local SSD cannot
        # spend the last free space during the half-second probe interval.
        with self.space_lock:
            if self.free_credit is not None:
                self.free_credit -= size
        self.check()

    def health(self):
        if self.finished is not None:return {'warning':'','seconds_without_progress':0}
        age = max(0, time.monotonic()-self.last_progress)
        warning = ''
        if self.paused and not self.pause_acknowledged:
            warning = 'Пауза запрошена. Приложение ждёт завершения текущего чтения или записи на устройстве.'
        elif not self.paused and not self.stopped and not self.expected_waiters and not self.network.offline.is_set() and age >= 120:
            warning = 'Давно нет прогресса. Возможно, накопитель или Яндекс.Диск не отвечает. Можно запросить паузу или остановку; новую копию задания до остановки запускать нельзя.'
        return {'warning':warning, 'seconds_without_progress':int(age) if warning else 0}

    @contextmanager
    def expected_wait(self):
        with self.lock:self.expected_waiters += 1
        try:
            yield
        finally:
            with self.lock:
                self.expected_waiters -= 1
                self.last_progress = time.monotonic()

    def pause(self, source='user'):
        with self.condition:
            # A destination probe must never override a user/policy/space pause.
            if source == 'user' or not self.paused or self.pause_kind in ('storage','storage_manual'):
                if source == 'user' or source != self.pause_kind or not self.paused:
                    self.pause_serial += 1
                self.pause_kind = source
            if not self.paused:
                self.paused = True
                self.pause_acknowledged = False
                self.pause_started = time.monotonic()
            self.condition.notify_all()
        self.network.pause_requests()

    def resume_storage(self, serial):
        with self.condition:
            if self.stopped or not self.paused or self.pause_kind != 'storage' or serial != self.pause_serial:
                return False
            self.resume()
            return True

    def storage_snapshot(self):
        with self.condition:
            waiting = self.paused and self.storage_problem and not self.stopped
            return {'waiting':waiting, 'automatic':waiting and self.pause_kind == 'storage',
                    'attempt':self.storage_retry_attempt,
                    'retry_in':max(0,self.storage_retry_due-time.monotonic()) if waiting and self.storage_retry_due is not None else None,
                    'message':self.storage_problem_reason if waiting else ''}

    def resume(self):
        with self.condition:
            if self.pause_started is not None:
                self.elapsed_before += time.monotonic() - self.pause_started
                self.pause_started = None
            self.paused = False
            self.pause_kind = ''
            self.storage_retry_due = None
            self.pause_acknowledged = False
            self.last_progress = time.monotonic()
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.paused = False
            self.pause_reason = ''
            self.condition.notify_all()
        self.network.abort_requests()

    def progress(self, path, done, size, action, **details):
        with self.lock:
            value = {"path": path, "done": done, "size": size, "action": action, **details}
            if self.active.get(path) != value:
                self.last_progress = time.monotonic()
            self.active[path] = value

    def snapshot(self):
        with self.lock:
            elapsed = self.active_clock() - self.started - self.network.elapsed
            verification = dict(self.verification) if self.verification and getattr(self,'phase','preparing')=='preparing' else None
            if verification:
                verification['eta_seconds'] = self.verification_time.estimate(verification['checked'], verification['total'])
            session = {'id':self.session_id, 'started_at':self.session_started_at,
                       'elapsed_seconds':max(0, elapsed), 'transferred_bytes':self.transferred,
                       'saved_bytes':self.session_saved_bytes, 'originals':self.session_originals,
                       'previews':self.session_previews, 'errors':self.session_errors,
                       'checked_files':self.verification['checked'] if self.verification else 0,
                       'check_total':self.verification['total'] if self.verification else None}
            return {"timings":{key:round(value, 3) for key,value in self.timings.items()}, "session":session, "active": list(self.active.values()), "transferred": self.transferred, "speed": self.transferred / max(0.1, elapsed), 'verification':verification, 'health':self.health()}

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
        from .performance import MemoryCache
        self.memory_cache = MemoryCache()
        from .traffic import NetworkUsage
        self.traffic = NetworkUsage(store)
        self.api.traffic = self.traffic
        self.lock = threading.RLock()
        self.controls = {}
        self.threads = {}
        self.journal_interval = 300
        self.partials = Partials(store)
        self.shutting_down = False
        self.folder_usage = FolderUsage()
        self.maintenance_busy = False
        self.history_restore_busy = False
        self.status_cache = StatusCache(self)
        self.status_options = threading.local()

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
        if missing_policy not in ("originals", "any_result", "both"):
            raise ValueError("Выберите, что считать скачанным: только оригинал или оригинал и превью.")
        values["missing_policy"] = missing_policy
        verification_mode = data.get('verification_mode','fast')
        if verification_mode not in ('fast','full'):
            raise ValueError('Выберите быструю или полную проверку скачанных файлов.')
        values['verification_mode'] = verification_mode
        local_workers = data.get('verification_workers', 4)
        if isinstance(local_workers, bool) or not isinstance(local_workers, int) or not 1 <= local_workers <= 4:
            raise ValueError('Проверка файлов на устройстве: выберите от 1 до 4 потоков.')
        values['verification_workers'] = local_workers
        values["retries"] = values["attempts"] - 1
        for name, default in (("create_zip", True), ("auto_pause", True), ("resume_parts", True), ("adaptive_workers", False), ("deduplicate", False), ("prevent_sleep", False), ("auto_start", False), ("economy_mode", False), ("early_download", False)):
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
        from .selection_rules import validate as selection_settings
        values.update(selection_settings(data))
        from .work_policy import validate as policy_settings
        values.update(policy_settings(data))
        return {"mode": mode, "preview_size": size, "reports": sorted(set(reports)), "file_types": sorted(set(file_types)),
                "paths": normalize_selection(paths, data.get("whole_disk", False)), "whole_disk": data.get("whole_disk", False),
                "destination": destination, "operation": operation, **values}

    def ensure_idle(self):
        if self.maintenance_busy:
            raise ValueError('Приложение на ПК выполняет проверку файлов или восстанавливает историю. Дождитесь завершения, затем повторите действие.')
        recovery = getattr(self,'recovery',None)
        if recovery and (recovery.snapshot()['busy'] or recovery.snapshot()['error']):
            raise ValueError('Сначала дождитесь восстановления после сбоя или нажмите «Повторить восстановление». Сохранённые файлы проверяются на ПК.')
        if any(thread.is_alive() for thread in self.threads.values()):
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
        # The same unfinished job already owns a durable page cursor. Reuse it
        # when the selected folder and scan filters match, rather than importing
        # its files into a fresh job and restarting the cloud walk at zero.
        if (previous and previous.get('store') is self.store and not previous['job']['scan_complete'] and not previous['job'].get('archived')
                and os.path.normcase(os.path.abspath(previous['job']['root'])) == os.path.normcase(os.path.abspath(settings['destination']))):
            keys = ('paths','whole_disk','file_types','exclusions','date_from','date_to','date_field','include_unknown_dates')
            old = previous['job']['settings']
            normalized = self.validate_settings(old)
            if all(normalized.get(key) == settings.get(key) for key in keys):
                resumed = previous['id']
                preserved = {key:value for key,value in old.items() if key not in settings}
                self.store.update_job(resumed,settings=json.dumps(preserved | settings,ensure_ascii=False))
                self.launch(resumed,'download' if settings['auto_start'] else 'scan')
                return resumed
        if previous:
            if sorted(previous['job']['settings'].get('paths',[])) != sorted(settings['paths']):
                settings['destination_warning'] = 'Выбор исходных папок отличается от прежней загрузки в эту папку. Проверьте выбранные пути перед запуском. Прежние файлы останутся на месте.'
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
            self.launch(job_id, "download" if settings['auto_start'] else "scan")
        except Exception:
            with self.store.db() as db:
                for table in ("entries", "names", "artifacts", "partials", "partial_ownership", "baseline", "changes", "verification_cache", "scan_errors", "journal_events", "journal_jobs"):
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

    def launch(self, job_id, operation="download", paths=None, selection=None):
        with self.lock:
            if self.shutting_down:
                raise ValueError("Приложение завершается")
            if any(t.is_alive() for t in self.threads.values()):
                raise ValueError("Дождитесь завершения текущей операции или остановите её")
            if operation not in ('pack','manifest','journal') and not self.api.credentials.connected:
                raise ValueError("Подключите Диск перед продолжением")
            control = Control()
            control.operation = operation
            control.selection = selection
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
            control.storage_check = lambda: self.check_space(job_id, control, force=True)
            control.destination = DestinationGuard(job['root'])
            control.floor_latched = job.get('pause_reason','').startswith('Мало места на устройстве:')
            control.resume_snapshot = job['phase'] in ('stopped','interrupted','failed','paused','pausing','stopping') or not job['scan_complete']
            control.selected_paths = tuple(paths) if paths else None
            control.reuse_scanned = operation == "download" and not paths and (job["settings"].get("operation") == "missing" or job["phase"] in ("paused", "stopped", "interrupted", "failed") or job["generation"] > 0)
            if operation == 'download' and not paths:
                with self.store.db() as db:
                    db.execute("UPDATE job_queue SET state='running',error='' WHERE job=?", (job_id,))
            self.controls[job_id] = control
            thread = threading.Thread(target=self.run, args=(job_id, operation, control), daemon=True)
            self.threads[job_id] = thread
            try:
                thread.start()
            except RuntimeError:
                self.controls.pop(job_id,None)
                self.threads.pop(job_id,None)
                with self.store.db() as db:
                    if selection:db.execute('DELETE FROM run_selection WHERE run=?',(selection,))
                    db.execute("UPDATE job_queue SET state='waiting',error='' WHERE job=? AND state='running'",(job_id,))
                    db.execute("UPDATE jobs SET phase='interrupted',error=? WHERE id=?",('Приложение на ПК не смогло начать работу. Закройте лишние программы и нажмите «Продолжить».',job_id))
                raise ValueError('Приложение на ПК не смогло начать работу. Закройте лишние программы и повторите запуск задания.') from None

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
            if self.maintenance_busy:
                raise ValueError('Приложение на ПК выполняет проверку файлов или восстанавливает историю. Дождитесь завершения, затем повторите действие.')
            job = self.store.job(job_id)
            scan_only = action == 'continue_scan'
            if scan_only:
                if job['scan_complete'] and not job['settings'].get('scan_only_pending'):
                    raise ValueError('Обход уже завершён. Нажмите «Начать скачивание».')
                action = 'resume'
            if job["settings"].get("_invalid") and action not in ("stop",):
                raise ValueError("Настройки задания повреждены. Создайте дозагрузку в прежнюю папку.")
            if action in ("start", "resume", "retry", "retry_failed", "retry_group", "retry_selected", "pack", "save_manifest", "refresh_journal"):
                own = self.threads.get(job_id)
                if not (action == "resume" and own and own.is_alive() and self.controls[job_id].paused):
                    self.ensure_idle()
            live = self.threads.get(job_id)
            control = self.controls.get(job_id)
            if action == "finish_current":
                if not live or not live.is_alive() or control.paused or job['phase'] != 'downloading':
                    raise ValueError('Кнопка доступна во время скачивания. Для обхода или паузы используйте «Остановить».')
                control.finish_current = True
                control.event('system', 'Завершаем текущие файлы. Новые файлы не начинаем.')
                return
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
                        db.execute("UPDATE jobs SET phase='stopping',pause_reason='' WHERE id=? AND phase IN ('new','scanning','preparing','downloading','packing','paused')", (job_id,))
                    control.event("system", "Запрошена остановка задания", level="warning")
                else:
                    self.store.update_job(job_id, phase="stopped", ended=now(), pause_reason="")
                    with self.store.db() as db:
                        db.execute("UPDATE entries SET status='cancelled' WHERE job=? AND kind='file' AND status IN ('pending','running')", (job_id,))
                    if control and control.journal:
                        control.event("system", "Задание остановлено", level="warning")
                        control.journal.flush()
                return
            if action == "resume" and live and live.is_alive() and control.paused:
                if limits and any(limits.get(k, job["settings"].get(k, 0 if k == "speed_limit_mib" else None)) != job["settings"].get(k, attempt_count(job["settings"]) if k == "attempts" else 0 if k == "speed_limit_mib" else None) for k in ("workers", "attempts", "timeout", "total_timeout", "speed_limit_mib")):
                    raise ValueError("Для изменения ограничений остановите задание и продолжите с новыми параметрами")
                if not self.api.credentials.connected and control.requires_connection():
                    raise ValueError("Для продолжения загрузки подключите Яндекс.Диск: вставьте ключ доступа вверху страницы. Скачанные файлы сохранены.")
                if limits and any(limits.get(key,job['settings'].get(key,default)) != job['settings'].get(key,default) for key,default in (('deduplicate',False),('verification_mode','fast'))):
                    raise ValueError('Чтобы изменить проверку или защиту от дубликатов, нажмите «Остановить», дождитесь остановки и затем «Продолжить».')
                if limits and limits.get("missing_policy", job["settings"].get("missing_policy", "originals")) != job["settings"].get("missing_policy", "originals"):
                    raise ValueError("Чтобы изменить учёт превью, нажмите «Остановить», дождитесь остановки и затем «Продолжить».")
                if limits:
                    keys = ("create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts", "speed_limit_mib")
                    validated = self.validate_settings(job["settings"] | {key: limits[key] for key in keys if key in limits})
                    job["settings"].update({key: validated[key] for key in keys})
                    self.store.update_job(job_id, settings=json.dumps(job["settings"], ensure_ascii=False))

                if (control.pause_reason or control.storage_problem) and self.check_space(job_id, control, force=True):
                    raise ValueError(control.pause_reason)
                self.store.update_job(job_id, phase=getattr(control, "phase", "downloading"), pause_reason="")
                control.pause_reason = ""
                control.resume()
                control.event("system", "Работа продолжена после паузы")
                return
            if action in ("start", "resume", "retry", "retry_failed", "retry_group", "retry_selected"):
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
                    keys = ("workers", "attempts", "timeout", "total_timeout", "create_zip", "auto_pause", "max_disk_percent", "adaptive_workers", "resume_parts", "speed_limit_mib", "missing_policy", "verification_mode", "verification_workers", "deduplicate")
                    validated = self.validate_settings({**job["settings"], **{k: limits[k] for k in keys if k in limits}})
                    job["settings"].update({k: validated[k] for k in (*keys, "retries")})
                    self.store.update_job(job_id, settings=json.dumps(job["settings"], ensure_ascii=False))
                selection = None
                with self.store.db() as db:
                    if action == 'retry_group':
                        error = (limits or {}).get('problem_error')
                        if not isinstance(error, str) or not error or len(error) > 20000:
                            raise ValueError('Выберите группу ошибок и повторите файлы этой группы.')
                        selection = uuid.uuid4().hex
                        db.execute("INSERT INTO run_selection SELECT ?,path FROM entries WHERE job=? AND kind='file' AND error=?", (selection, job_id, error))
                        if not db.execute('SELECT 1 FROM run_selection WHERE run=?', (selection,)).fetchone():
                            raise ValueError('В этой группе больше нет файлов с ошибкой. Обновите список проблем.')
                        db.execute("UPDATE entries SET status='pending',error='' WHERE job=? AND path IN (SELECT path FROM run_selection WHERE run=?)", (job_id, selection))
                    elif action == 'retry_failed':
                        selection = uuid.uuid4().hex
                        db.execute("INSERT INTO run_selection SELECT ?,path FROM entries WHERE job=? AND kind='file' AND status IN ('error','partial','no_preview','cancelled')", (selection,job_id))
                        if not db.execute('SELECT 1 FROM run_selection WHERE run=?',(selection,)).fetchone():
                            raise ValueError('Файлов с ошибками нет. Для продолжения незавершённого задания нажмите «Продолжить».')
                        db.execute("UPDATE entries SET status='pending',error='' WHERE job=? AND path IN (SELECT path FROM run_selection WHERE run=?)",(job_id,selection))
                    elif action == "retry_selected":
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
                self.launch(job_id, "scan" if scan_only else "download", paths=paths if action == "retry_selected" else None, selection=selection)
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
        if hasattr(self.api, 'json_connections'):
            self.api.json_connections.close_idle()
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

    @contextmanager
    def cached_storage_status(self):
        previous = getattr(self.status_options,'cached_storage',False)
        self.status_options.cached_storage = True
        try:yield
        finally:self.status_options.cached_storage = previous

    def status(self, job_id):
        self.reconcile(job_id)
        live = bool(self.threads.get(job_id) and self.threads[job_id].is_alive())
        status = self.store.summary(job_id)
        control = self.controls.get(job_id)
        status.update(control.snapshot() if control else {"active": [], "speed": 0})
        status["live"] = live
        status['storage'] = control.storage_snapshot() if control else None
        status['requires_connection'] = not (live and control and not control.requires_connection())
        if not live or control and control.paused:
            status["speed"] = 0
        finishing = status["phase"] in ("completed", "completed_with_errors", "stopped", "failed", "ready")
        if live and control and control.stopped and status['phase']=='stopped':
            status['phase'] = 'stopping'
            finishing = False
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
        # Use recorded output sizes; do not stat thousands of old report names.
        from .service_outputs import root_key
        output_root = os.path.normcase(os.path.abspath(status['root'])) if getattr(self.status_options,'cached_storage',False) else root_key(status['root'])
        for row in self.store.rows('SELECT s.local,s.signature FROM service_output_signatures s JOIN names n ON n.job=? AND n.identity=\'@output:\'||s.local AND n.local=s.local WHERE s.root=?', (job_id, output_root)):
            try:
                service_bytes += max(0, int(json.loads(row['signature'])[2]))
                counted_outputs.add(row['local'])
            except (ValueError, TypeError, IndexError):
                pass
        if getattr(self.status_options,'cached_storage',False):
            if status['settings'].get('manifest_local') not in counted_outputs:
                service_bytes += status['settings'].get('manifest_bytes',0)
            # HTTP statistics use committed sizes and live write counters. Actual
            # filesystem accounting belongs to the bounded background inventory.
            if status['zip_path']:
                service_bytes += status.get('zip_bytes',0)
            temporary = ()
            if control:
                with control.lock:
                    temporary = tuple((filename, control.temporary_sizes.get(filename,0)) for filename in control.temporary)
                service_bytes += sum(size for filename,size in temporary)
            active_temporary = {name for name,size in temporary}
            temporary_sizes = dict(temporary)
            for part in self.store.rows('SELECT local,size FROM partials WHERE job=?',(job_id,)):
                filename = native(Path(status['root'])/part['local'])
                written = temporary_sizes.get(filename,part['size'])
                status['partial_bytes'] += written
                if filename not in active_temporary:
                    service_bytes += written
        else:
            for name in ('manifest_local', 'changes_local'):
                if status['settings'].get(name) and status['settings'][name] not in counted_outputs:
                    try:
                        service_bytes += os.path.getsize(safe_join(status['root'], status['settings'][name]))
                    except (OSError, PathError):
                        pass
            if status["zip_path"]:
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
        usage = self.folder_usage.for_job(self.store, job_id, status["root"], active=live and not (control and control.paused))
        status["folder_bytes"] = usage['bytes'] if usage['bytes'] is not None else None if usage.get('error') else status['local_bytes']
        status['folder_bytes_error'] = usage.get('error','')
        status["folder_bytes_scanning"] = usage["scanning"]
        status["folder_bytes_updated"] = usage["updated"]
        status['folder_bytes_partial'] = usage.get('partial',False)
        status['folder_files_counted'] = usage.get('counted_files',0)
        status["eta_seconds"] = None
        status["eta_phase"] = None
        status["eta_message"] = "Ожидание интернета" if status["network_waiting"] else "Уточняется…"
        status["eta_basis"] = None
        if live and control and not status["network_waiting"] and status["phase"] in ("downloading", "packing"):
            phase = status["phase"]
            work = status["remaining_work"]
            remaining_bytes, unknown_files = work["bytes"], work["unknown_files"]
            want_both = status['settings'].get('missing_policy') == 'both' or status["settings"]["mode"] == "both" and status["settings"].get("operation") != "missing" and not status["generation"]
            if want_both:
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
                    original_only = not want_both and (status["settings"]["mode"] == "fallback" or status["settings"].get("operation") == "missing" or status["generation"] > 0)
                    status["eta_seconds"] = control.remaining_time.estimate(phase, control.transferred, status["processed"], remaining_bytes, work["files"], unknown_files, original_only=original_only)
                status["eta_message"] = control.remaining_time.reason
                status["eta_basis"] = control.remaining_time.basis
            status["eta_phase"] = phase
        elif status["phase"] in ("completed", "completed_with_errors") and not live:
            status["eta_seconds"] = 0
        if getattr(self.status_options,'cached_storage',False):
            if control and control.destination:
                with control.destination.condition:
                    value = control.destination.value
                    status['free_bytes'] = value.free if value is not None else None
            else:
                status['free_bytes'] = usage.get('free_bytes')
        else:
            try:
                status["free_bytes"] = shutil.disk_usage(status["root"]).free
            except OSError:
                status["free_bytes"] = None
        return status

    def phase(self, job_id, control, phase):
        control.phase = phase
        control.last_progress = time.monotonic()
        if phase in ("downloading", "packing"):
            control.remaining_time.start(phase, 0 if phase == "packing" else control.transferred, self.store.summary(job_id)["processed"])
        self.store.update_job(job_id, phase="paused" if control.paused else phase)
        control.event("archive" if phase == "packing" else "system", PHASES.get(phase, phase))

    def check_space(self, job_id, control, force=False):
        with control.space_lock:
            if control.stopped:raise Suspended('Задание остановлено')
            if control.paused and not force:return True
            floor_check = control.free_credit is not None and control.free_credit < HARD_FREE_BYTES and (not control.space_draining or not control.transfer_sizes)
            if not force and not floor_check and time.monotonic() < control.next_space_check:
                return control.paused
            refresh = force or control.next_space_check == 0
            control.next_space_check = time.monotonic() + 0.5
            job = self.store.job(job_id)
            if control.destination:
                generation = control.destination.generation
                usage, error = control.destination.check(lambda: control.stopped, force=refresh or floor_check)
                fresh_usage = generation != control.destination.generation
                if control.stopped:
                    raise Suspended('Задание остановлено')
                if error or usage is None:
                    reason = error or 'Папка загрузки недоступна. Подключите накопитель и нажмите «Продолжить».'
                    changed = control.pause_reason != reason
                    control.storage_problem = True
                    control.storage_problem_reason = reason
                    control.pause_reason = reason
                    control.pause(source='storage' if control.destination.snapshot()['recoverable'] else 'storage_manual')
                    self.start_storage_recovery(job_id, control)
                    if changed:
                        self.store.update_job(job_id,phase='paused',pause_reason=reason)
                        control.event('system',reason,level='warning')
                    return True
            else:
                usage = shutil.disk_usage(job['root'])
                fresh_usage = True
            control.storage_problem = False
            control.storage_problem_reason = ''
            control.next_space_check = time.monotonic()+.5
            if fresh_usage or control.free_credit is None:
                control.free_credit = usage.free
            free = min(usage.free, control.free_credit)
            floor = RESUME_FREE_BYTES if control.floor_latched else HARD_FREE_BYTES
            if free < floor:
                if not control.floor_latched and getattr(control,'phase','') == 'downloading' and may_finish(control.transfer_sizes, free):
                    if not control.space_draining:
                        control.space_draining = True
                        control.event('system','Осталось меньше 500 МБ. Новые файлы не начинаем: завершаем небольшие текущие загрузки, затем включим паузу.',level='warning')
                    return False
                reason = 'Мало места на устройстве: осталось меньше 500 МБ или ещё не восстановлен запас. Скачивание на паузе. Освободите не менее 1 ГБ и нажмите «Продолжить».'
                changed = control.pause_reason != reason
                control.floor_latched = True
                control.space_draining = False
                control.pause_reason = reason
                control.pause(source='space')
                if changed:
                    self.store.update_job(job_id,phase='paused',pause_reason=reason)
                    control.event('system',reason,level='warning')
                return True
            control.space_draining = False
            control.floor_latched = False
            if not job["settings"].get("auto_pause", True):
                return False
            occupied = (usage.total - usage.free) * 100 / max(1, usage.total)
            threshold = job["settings"].get("max_disk_percent", 95)
            if occupied >= threshold:
                reason = f"Автопауза: диск заполнен на {occupied:.1f}%, порог {threshold:g}%. Освободите место и нажмите «Продолжить»."
                if control.pause_reason != reason:
                    control.pause_reason = reason
                    control.pause(source='space')
                    self.store.update_job(job_id, phase="paused", pause_reason=reason)
                    control.event("system", reason, level="warning")
                return True
            return False

    def start_storage_recovery(self, job_id, control):
        # Called under the destination/space lock: at most one monitor per run.
        if control.pause_kind != 'storage' or control.stopped:
            return
        if control.storage_retry_thread and control.storage_retry_thread.is_alive():
            return
        def recover():
            try:
                while True:
                    with control.condition:
                        if control.stopped or not control.paused or control.pause_kind != 'storage':return
                        serial = control.pause_serial
                        delay = control.storage_retry_delays[min(control.storage_retry_attempt,len(control.storage_retry_delays)-1)]
                        control.storage_retry_attempt += 1
                        control.storage_retry_due = time.monotonic()+delay
                        while not control.stopped and control.paused and control.pause_kind == 'storage' and serial == control.pause_serial:
                            left = control.storage_retry_due-time.monotonic()
                            if left <= 0:break
                            control.condition.wait(left)
                        if control.stopped or not control.paused or control.pause_kind != 'storage' or serial != control.pause_serial:return
                    if self.check_space(job_id,control,force=True):
                        continue
                    with self.lock:
                        if self.shutting_down or self.controls.get(job_id) is not control:return
                        self.store.update_job(job_id,phase=getattr(control,'phase','downloading'),pause_reason='')
                        if control.resume_storage(serial):
                            control.pause_reason = ''
                            control.storage_retry_attempt = 0
                            control.event('system','Накопитель снова доступен. Прежняя папка проверена; продолжаем работу.',level='success')
                        else:
                            self.store.update_job(job_id,phase='stopping' if control.stopped else 'paused',pause_reason=control.pause_reason)
                    return
            except Suspended:
                pass
            except Exception as exc:
                # Remain safely paused; a failed monitor never resumes writes.
                self.failure_message(exc,'storage_recovery',control)
                with control.condition:
                    if control.pause_kind == 'storage':control.pause_kind = 'storage_manual'
                control.event('system','Приложение на устройстве не смогло повторно проверить накопитель. Работа остаётся на паузе. Проверьте подключение и нажмите «Продолжить».',level='warning')
            finally:
                with control.space_lock:
                    with control.condition:
                        control.storage_retry_due = None
                        control.storage_retry_thread = None
                    if not self.shutting_down and self.controls.get(job_id) is control:
                        self.start_storage_recovery(job_id,control)
        control.storage_retry_thread = threading.Thread(target=recover,name='disk-storage-recovery',daemon=True)
        try:control.storage_retry_thread.start()
        except RuntimeError:
            control.pause_kind = 'storage_manual'
            control.event('system','Не удалось запустить повторную проверку накопителя. Проверьте подключение и нажмите «Продолжить».',level='warning')

    def failure_message(self, exc, operation, control):
        message = safe_error(exc)
        recorder = getattr(self, 'request_errors', None)
        if recorder:
            reference = uuid.uuid4().hex[:8]
            try:
                # Record before cleanup, which may itself fail on the same DB.
                recorder.record(reference, 'job_'+operation, exc, phase=getattr(control, 'phase', 'starting'))
            except Exception:
                # Diagnostic IO must never replace the actual task failure.
                pass
            else:
                message += ' Номер ошибки: ' + reference + '.'
        return message

    def run(self, job_id, operation, control):
        try:
            from .power import SleepGuard
            from .work_policy import Policy
            settings = self.store.job(job_id)['settings']
            control.policy = Policy(settings, control)
            with self.store.activity(), SleepGuard(control, settings.get('prevent_sleep',False)):
                self._run(job_id, operation, control)
        except Exception as exc:
            # Includes failures opening the activity connection / final journal.
            # The next status request also reconciles a dead worker's phase.
            message = self.failure_message(exc, operation, control)
            try:
                self.store.update_job(job_id, phase="failed", ended=now(), error=message)
            except sqlite3.Error:
                pass
            finally:
                with control.lock:
                    control.active.clear()
                    control.finished = time.monotonic()

        finally:
            if getattr(control,'selection',None):
                try:
                    with self.store.db() as db:db.execute('DELETE FROM run_selection WHERE run=?',(control.selection,))
                except sqlite3.Error:pass

    def _run(self, job_id, operation, control):
        saved_scan = False
        try:
            previous_job = self.store.job(job_id)
            self.store.update_job(job_id, error="", ended=None, pause_reason="")
            job = self.store.job(job_id)
            control.check()
            if job['settings'].get('_inherit_source'):
                self.phase(job_id, control, 'preparing')
                source = job['settings']['_inherit_source']
                if source.get('job'):
                    previous = {'job':self.store.job(source['job']),'store':self.store,'id':source['job']}
                else:
                    previous = read_import(job['root'], source['manifest'])
                if not previous:
                    raise ValueError('На ПК не найдена прежняя опись загрузки. Проверьте папку или создайте дозагрузку заново.')
                control.progress(job['root'],0,None,'Готовим проверку скачанных файлов',unit='local_files')
                inherited = inherit(self.store,job_id,previous,control.check,details=lambda count,total,stage:control.progress(job['root'],count,total,'Подготовка списка скачанных файлов' if stage=='files' else 'Готовим проверку скачанных файлов',unit='local_files')) or {}
                settings = self.store.job(job_id)['settings']
                if inherited.get('unreadable_metadata'):
                    if not previous.get('store'):
                        settings['history_backup_local'] = backup_import(self.store,job_id,previous,control.check)
                    settings['history_warning'] = 'Часть прежней истории не удалось прочитать. При дозагрузке используются свежие данные Яндекс.Диска и проверка файлов на устройстве. Сравнение этих записей со старой загрузкой недоступно.'
                    control.event('system',settings['history_warning'],level='warning')
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
                from .work_policy import due
                early = (job['settings'].get('early_download') and operation == 'download'
                         and job['settings'].get('operation') == 'new' and not job['settings'].get('deduplicate')
                         and not job['settings'].get('priority_paths') and due(job['settings']))
                if early:
                    def page_ready():
                        self.phase(job_id, control, 'preparing')
                        self.prepare_paths(job_id, control, incremental=True)
                        self.phase(job_id, control, 'downloading')
                        self.download(job_id, control)
                        self.phase(job_id, control, 'scanning')
                    control.page_ready = page_ready
                self.phase(job_id, control, "scanning")
                try:
                    self.scan(job_id, control)
                finally:
                    control.page_ready = None
            self.phase(job_id, control, "preparing")
            self.prepare_paths(job_id, control, prime=operation == "scan" or getattr(control, "reuse_scanned", False))
            if operation == "scan":
                save_import(self.store, job_id, control.check)
                saved_scan = True
                self.store.update_job(job_id, phase="ready")
                return
            from .work_policy import due
            if job['settings'].get('auto_start') and not due(job['settings']):
                save_import(self.store,job_id,control.check)
                saved_scan = True
                self.store.update_job(job_id,phase='ready')
                enqueue = getattr(self,'enqueue_after_scan',None)
                if enqueue:
                    enqueue(job_id)
                control.event('system','Проверка завершена. Загрузка ждёт выбранного времени запуска.')
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
            message = self.failure_message(exc, operation, control)
            with self.store.db() as db:
                db.execute("UPDATE entries SET status='pending' WHERE job=? AND status='running'", (job_id,))
            self.store.update_job(job_id, phase="failed", ended=now(), error=message)
            control.event("errors", message, level="error")
        finally:
            control.event("system", PHASES.get(self.store.job(job_id)["phase"], "Операция завершена"))
            control.journal.close()
            try:
                if not saved_scan and not self.store.job(job_id)['settings'].get('_inherit_source'):
                    from .destination import export_available
                    if not export_available(control):
                        raise OSError('Destination unavailable; progress remains in application history')
                    save_import(self.store, job_id)
            except (OSError, sqlite3.Error, ValueError) as exc:
                warning = 'Не удалось обновить опись в папке на ПК. Файлы и история сохранены в приложении. Проверьте доступ к папке и свободное место; затем повторите сохранение описи.'
                control.journal.error = warning
                self.failure_message(exc, 'manifest', control)
                current = self.store.job(job_id)
                # A secondary export failure must not hide the original cause.
                if not current['error']:
                    self.store.update_job(job_id, error=warning)
                if current['phase'] == 'completed':
                    self.store.update_job(job_id, phase='completed_with_errors')
            with control.lock:
                control.active.clear()
                control.finished = time.monotonic()

            # Completed worker pools cannot reuse their thread-owned sockets.
            if hasattr(self.api, 'json_connections'):
                self.api.json_connections.close_idle()

            # Refresh only downloaded media, never walk service output folders.
            if not self.shutting_down and operation != 'scan':
                self.folder_usage.for_job(self.store, job_id, self.store.job(job_id)['root'], force=True)

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
        from .scanner import scan_pages
        scan_pages(self, job_id, settings, control)
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

    def allocate_publication_name(self, job_id, identity, proposed):
        parent, _, name = proposed.rpartition("/")
        if len(name.encode("utf-8")) > 255:
            name = suffix(name, digest(identity))
            proposed = (parent + "/" if parent else "") + name
        relative = self.allocate(job_id, identity, proposed)
        if len(relative.rsplit("/", 1)[-1].encode("utf-8")) > 255:
            # Preserve legacy allocations and files; only new publications
            # need a separate portable name, including collision versions.
            relative = self.allocate(job_id, "@bounded:" + identity, proposed)
        return relative

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

    def prepare_paths(self, job_id, control=None, prime=False, incremental=False):
        job = self.store.job(job_id)
        cursor = ""
        count = 0
        cache = {"disk:/": ""}
        while True:
            if control:
                control.check()
            with self.store.db() as db:
                scope = "local IS NULL" if incremental else "(local IS NULL OR kind='dir')"
                rows = db.execute("SELECT path,kind,meta,local FROM entries WHERE job=? AND path>? AND " + scope + " ORDER BY path COLLATE BINARY LIMIT 500", (job_id, cursor)).fetchall()
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
                            proposed = (relative + "/" if relative else "") + component(part, source)
                            relative, counter = proposed, 0
                            while True:
                                inserted = db.execute("INSERT OR IGNORE INTO names VALUES(?,?,?,?)", (job_id, source, relative, relative.casefold())).rowcount
                                if inserted:
                                    break
                                existing = db.execute("SELECT local FROM names WHERE job=? AND identity=?", (job_id, source)).fetchone()
                                if existing:
                                    relative = existing[0]
                                    break
                                counter += 1
                                parent, _, name = proposed.rpartition("/")
                                relative = (parent + "/" if parent else "") + suffix(name, digest(source + str(counter)))
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

    def live_limits(self, job_id, data):
        with self.lock:
            if self.maintenance_busy:
                raise ValueError('Приложение на ПК выполняет проверку файлов или восстанавливает историю. Дождитесь завершения, затем повторите действие.')
            if not isinstance(data, dict) or not data or set(data)-{'workers','speed_limit_mib'}:
                raise ValueError('На ходу можно менять только потоки и скорость')
            job = self.store.job(job_id)
            updated = self.validate_settings(job['settings'] | data)
            job['settings'].update({key:updated[key] for key in data})
            self.store.update_job(job_id, settings=json.dumps(job['settings'],ensure_ascii=False))
            control = self.controls.get(job_id)
            if control:
                with control.lock:
                    control.workers = updated['workers']
                if getattr(control,'policy',None):
                    control.policy.update_speed(updated['speed_limit_mib'])
                else:
                    control.rate_limiter.configure(updated['speed_limit_mib'])
                control.event('system','Изменены потоки и ограничение скорости')
            return {'workers':updated['workers'],'speed_limit_mib':updated['speed_limit_mib']}

    def download(self, job_id, control):
        workers = self.store.job(job_id)["settings"]["workers"]
        first_stage_work = False
        priorities = self.store.job(job_id)['settings'].get('priority_paths',[])
        for path in priorities:
            prefix=path.rstrip('/')+'/'
            cursor=''
            while True:
                control.check()
                with self.store.db() as db:
                    rows=db.execute("SELECT path,user_priority FROM entries WHERE job=? AND kind='file' AND path>=? AND path<? AND path>? AND (path=? OR path>=?) ORDER BY path LIMIT 1000",(job_id,path,prefix[:-1]+'0',cursor,path,prefix)).fetchall()
                    if not rows:break
                    db.executemany('UPDATE entries SET user_priority=0 WHERE job=? AND path=?',((job_id,row['path']) for row in rows if row['user_priority']!=0))
                cursor=rows[-1]['path']
        control.workers = workers
        with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="disk-download") as pool:
            for stage in range(2):
                if stage and first_stage_work:
                    with control.lock:
                        control.remaining_time.new_stage()
                condition = "" if stage else " AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.job=entries.job AND a.path=entries.path)"
                parameters = (job_id,)
                if control.selected_paths:
                    condition += " AND path IN (" + ",".join("?" for _ in control.selected_paths) + ")"
                    parameters += control.selected_paths
                if getattr(control,'selection',None):
                    condition += ' AND path IN (SELECT path FROM run_selection WHERE run=?)'
                    parameters += (control.selection,)
                active = set()
                exhausted = False
                while True:
                    control.check()
                    capacity = (control.adaptive.limit() if control.adaptive else control.workers) - len(active)
                    if capacity > 0 and not control.space_draining and not control.finish_current and not exhausted:
                        with self.store.db() as db:
                            entries = db.execute("SELECT * FROM entries WHERE job=? AND kind='file' AND status='pending'" + condition + " ORDER BY priority,user_priority,path LIMIT ?", (*parameters, capacity)).fetchall()
                            db.executemany("UPDATE entries SET status='running' WHERE job=? AND path=?", ((job_id, entry['path']) for entry in entries))
                        exhausted = not entries
                        for entry in entries:
                            if not stage:
                                first_stage_work = True
                            active.add(pool.submit(self.process, job_id, dict(entry), control))
                    if not active:
                        if control.finish_current:
                            control.stop()
                            raise Suspended('Текущие файлы сохранены. Можно продолжить задание позже.')
                        break
                    done, active = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                    if done:
                        exhausted = False
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
        control = control or Control()
        # Only cached read-only probes run in parallel. Any changed, missing or
        # uncached file falls back to the ordinary serial hash/repair path.
        with self.store.session(), ThreadPoolExecutor(max_workers=job['settings'].get('verification_workers', 4),thread_name_prefix='disk-verify') as probes:
            try:
                control.available_probe_pool = probes if job['settings'].get('verification_mode','fast') != 'full' and not job['settings'].get('deduplicate') else None
                control.cached_probe_pool = control.available_probe_pool if str(job['root']).startswith(('\\\\','//')) else None
                control.cached_probe_costs = []
                with control.measure('local_check'):
                    return self._check_local_results(job, control)
            finally:
                if control:
                    control.verification_records = None
                    control.cached_results = None
                    control.cached_probe_pool = None
                    control.available_probe_pool = None

    def _probe_cached_copies(self, job, entry, records, control):
        import stat
        meta = json.loads(entry['meta'])
        result = {}
        for kind in ('original','preview'):
            control.check()
            if kind == 'preview' and result.get('original') and job['settings'].get('missing_policy') != 'both':
                break
            artifact = records.get((entry['path'],kind))
            if not artifact or not artifact.get('signature'):
                continue
            try:
                valid = artifact['fingerprint'] == fingerprint(meta)
                if kind == 'preview':
                    valid = valid and preview_type(meta) and artifact['size'] <= PREVIEW_MAX_BYTES and (artifact['preview_size'] == job['settings']['preview_size'] or job['settings'].get('missing_policy') == 'any_result')
                elif meta.get('sha256'):
                    valid = valid and artifact['sha256'].lower() == meta['sha256'].lower()
                if not valid:
                    continue
                filename = safe_join(job['root'],artifact['local'])
                signature = file_signature(filename,artifact['sha256'])
                if signature[-1] is None or signature[2] != artifact['size'] or artifact['signature'] != json.dumps(signature) or not stat.S_ISREG(os.stat(filename).st_mode):
                    continue
                control.check()
                if file_signature(filename,artifact['sha256']) == signature:
                    result[kind] = True
            except (OSError,ValueError):
                # Destination failures and changed content are handled by the
                # owner thread, preserving its pause and invalidation rules.
                continue
        return result

    def _check_local_results(self, job, control):
        """Finish local verification before any download workers are started."""
        control = control or Control()
        job_id = job["id"]
        selection = getattr(control,'selection',None)
        scope = ' AND path IN (SELECT path FROM run_selection WHERE run=?)' if selection else ''
        scope_args = (selection,) if selection else ()
        with self.store.db() as db:
            total = db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND kind='file' AND status!='filtered'"+scope, (job_id,*scope_args)).fetchone()[0]
        checked = found = 0
        originals = previews = 0
        with control.lock:
            control.verification_time.reset()
            control.verification_time.estimate(0, total)
            control.verification = {'checked':0,'total':total,'originals':0,'previews':0,'missing':0,'complete':not total,'cached':0,'hashed':0}
        cursor = ""
        updates, remove_previews = [], []
        last_flush = time.monotonic()
        def flush():
            nonlocal last_flush
            if not updates and not remove_previews:
                return
            with self.store.db() as db:
                db.executemany("UPDATE entries SET status=?,priority=?,error='',original_status=?,preview_status=?,fallback=0 WHERE job=? AND path=?", updates)
                db.executemany("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", remove_previews)
            updates.clear()
            remove_previews.clear()
            last_flush = time.monotonic()
        control.event("cache", "Проверка папки перед дозагрузкой")
        while True:
            control.check()
            with self.store.db() as db:
                rows = db.execute("SELECT * FROM entries WHERE job=? AND kind='file' AND status!='filtered' AND path>?"+scope+" ORDER BY path LIMIT 200", (job_id, cursor,*scope_args)).fetchall()
            if not rows:
                break
            with self.store.db() as db:
                artifacts = db.execute('SELECT a.*,v.signature FROM artifacts a LEFT JOIN verification_cache v ON v.job=a.job AND v.local=a.local WHERE a.job=? AND a.path>=? AND a.path<=?'+(' AND a.path IN (SELECT path FROM run_selection WHERE run=?)' if selection else ''), (job_id,rows[0]['path'],rows[-1]['path'],*scope_args)).fetchall()
            control.verification_records = {(row['path'],row['type']):dict(row) for row in artifacts}
            probes = {}
            pool = getattr(control,'cached_probe_pool',None)
            for index, row in enumerate(rows):
                control.check()
                probe_started, cached_before = time.monotonic(), control.verification_cached
                if pool:
                    for upcoming in range(index,min(index+4,len(rows))):
                        if upcoming not in probes:
                            probes[upcoming] = pool.submit(self._probe_cached_copies,job,dict(rows[upcoming]),control.verification_records,control)
                    future = probes.pop(index)
                    while True:
                        try:
                            control.cached_results = future.result(timeout=.1)
                            break
                        except TimeoutError:
                            control.check()
                entry = dict(row)
                control.progress(entry["path"], checked, total, "Проверка скачанной папки", unit="files")
                meta = json.loads(entry["meta"])
                original = preview = False
                try:
                    original = self.reusable(job, entry, "original", meta, control)
                    want_both = job['settings'].get('missing_policy') == 'both'
                    if original and job['settings'].get('deduplicate'):
                        from .duplicates import compact
                        compact(self,job,entry,'original',meta,control)
                    preview = (not original or want_both) and preview_type(meta) and self.reusable(job, entry, "preview", meta, control)
                    if preview and job['settings'].get('deduplicate'):
                        from .duplicates import compact
                        compact(self,job,entry,'preview',meta,control)
                    if (original or preview) and job["settings"].get("operation") != "missing" and not getattr(control,'resume_snapshot',False):
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
                    # A preview failure is not evidence that a verified original
                    # has vanished. Let reusable() invalidate only its own copy.
                    preview = False
                if original:
                    need_preview = want_both and preview_type(meta)
                    desired = ('pending' if need_preview and not preview else 'existing',2,'existing_verified','existing_verified' if preview else 'not_requested' if need_preview else 'not_needed')
                    if not want_both and (entry['path'],'preview') in control.verification_records:
                        self.remove_preview(job, entry['path'], control)
                elif preview and job['settings'].get('missing_policy','originals') == 'any_result':
                    desired = ('existing',1,'not_requested','existing_verified')
                else:
                    desired = ('pending',int(bool(preview)),'not_requested','existing_verified' if preview else 'not_requested')
                previous = tuple(entry[key] for key in ('status','priority','original_status','preview_status'))
                if previous != desired or entry['error'] or entry['fallback']:
                    updates.append((*desired,job_id,entry['path']))
                if not original or time.monotonic()-last_flush >= .5:
                    # Publish invalidations before checking a slow / unavailable
                    # next file. Verified result records remain durable per file.
                    flush()
                checked += 1
                found += bool(original or preview)
                originals += bool(original)
                previews += bool(preview and not original)
                with control.lock:
                    control.active.clear()
                    control.verification_time.estimate(checked,total)
                    control.verification = {'checked':checked,'total':total,'originals':originals,'previews':previews,'missing':checked-found,'complete':checked==total,'cached':control.verification_cached,'hashed':control.verification_hashed}
                control.progress(entry["path"], checked, total, "Проверка скачанной папки", unit="files")
                if not pool and getattr(control,'available_probe_pool',None) and control.verification_cached > cached_before:
                    control.cached_probe_costs.append(time.monotonic()-probe_started)
                    if len(control.cached_probe_costs) >= 8:
                        if sum(control.cached_probe_costs)/len(control.cached_probe_costs) >= .006:
                            pool = control.cached_probe_pool = control.available_probe_pool
                        else:
                            control.available_probe_pool = None
            cursor = rows[-1]["path"]
            flush()
        control.event("cache", f"Папка проверена: {checked} файлов, уже есть результат у {found}. Сначала будут скачаны файлы без результата.")

    def remove_preview(self, job, path, control):
        while not control.duplicate_cleanup_lock.acquire(timeout=.15):control.check()
        try:return self._remove_preview(job,path,control)
        finally:control.duplicate_cleanup_lock.release()

    def _remove_preview(self, job, path, control):
        """Keep modified/foreign files and retain their record when cleanup fails."""
        from .preview_cleanup import remove_owned
        with self.store.db() as db:
            entry = db.execute('SELECT * FROM entries WHERE job=? AND path=?',(job['id'],path)).fetchone()
            original = db.execute("SELECT a.*,v.signature FROM artifacts a LEFT JOIN verification_cache v ON v.job=a.job AND v.local=a.local WHERE a.job=? AND a.path=? AND a.type='original'", (job['id'], path)).fetchone()
            preview = db.execute("SELECT * FROM artifacts WHERE job=? AND path=? AND type='preview'", (job['id'], path)).fetchone()
        if not original or not preview or original['local'] == preview['local']:
            return
        with self.store.db() as db:
            shared=db.execute('SELECT COUNT(*) FROM artifacts WHERE job=? AND local=?',(job['id'],preview['local'])).fetchone()[0]>1
            if shared:
                db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'",(job['id'],path))
                return
        try:
            if not entry or not self.reusable(job,dict(entry),'preview',json.loads(entry['meta']),control):
                return
            original_file = safe_join(job['root'],original['local'])
            signature = file_signature(original_file,original['sha256'])
            if signature[2] != original['size']:
                return
            cached = signature[-1] is not None and original['signature'] == json.dumps(signature)
            if not cached and file_hash(original_file,control.check) != original['sha256']:
                return
            def checkpoint():
                control.check()
                if file_signature(original_file,original['sha256']) != signature:
                    raise OSError('Original changed during preview cleanup')
            checkpoint()
            if not remove_owned(job['root'], dict(preview), checkpoint):
                control.event('cache', 'Превью оставлено: файл изменён или открыт другой программой.', path, 'warning')
                return
        except OSError:
            control.event('cache', 'Оригинал сохранён. Превью пока не удалено: проверьте доступ к файлу на устройстве.', path, 'warning')
            return
        with self.store.db() as db:
            db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview' AND local=? AND sha256=?", (job['id'], path, preview['local'], preview['sha256']))
            db.execute('DELETE FROM verification_cache WHERE job=? AND local=?', (job['id'], preview['local']))
        control.event('cache', 'Оригинал сохранён и проверен. Прежнее превью удалено.', path, 'success')

    def remember_file(self, job, relative, sha):
        signature = file_signature(safe_join(job["root"], relative), sha)
        if signature[-1] is None:
            return
        signature = json.dumps(signature)
        # The file and artifact are already durable. Cache admission/commit is
        # optional; filesystem identity errors above must still propagate.
        try:
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO verification_cache VALUES(?,?,?)", (job["id"], relative, signature))
        except sqlite3.Error:
            return False
        return True

    def partials_digest(self, filename, size, control):
        from .partials import digest_prefix
        sha, md5 = digest_prefix(filename, size, control.check)
        return sha.hexdigest(), md5.hexdigest()

    def reusable(self, job, entry, kind, meta, control):
        while True:
            try:
                return self._reusable(job, entry, kind, meta, control)
            except StorageRetry:
                control.check()

    def _reusable(self, job, entry, kind, meta, control):
        if (getattr(control,'cached_results',None) or {}).get(kind):
            control.verification_cached += 1
            return True
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
            sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
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
                cached = job['settings'].get('verification_mode','fast') != 'full' and signature[-1] is not None and artifact.get('signature') == encoded
                if cached:
                    control.verification_cached += 1
                else:
                    control.verification_hashed += 1
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
                    with Image.open(filename, formats=PREVIEW_FORMATS) as image:
                        if image.width * image.height > 16_000_000:
                            valid = False
                        else:
                            image.verify()
                    if valid:
                        with Image.open(filename, formats=PREVIEW_FORMATS) as image:
                            image.load()
                valid = valid and signature == file_signature(filename, artifact["sha256"])
                if valid and not cached and signature[-1] is not None:
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO verification_cache VALUES(?,?,?)", (job["id"], artifact["local"], encoded))
        except (OSError, ValueError) as exc:
            if control.storage_error(exc):
                raise StorageRetry() from exc
            valid = False
        if valid:
            return True
        # Confirm the destination before invalidating a previously saved copy.
        # Missing NAS files are not evidence that the downloaded copy is gone.
        if control.destination and self.check_space(job['id'], control, force=True):
            control.check()
            raise StorageRetry()
        with self.store.db() as db:
            db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type=?", (job["id"], entry["path"], kind))
        return False

    def cache_scan_metadata(self, job_id, items, epoch):
        self.api.credentials.check(epoch)
        self.memory_cache.select_scope(epoch)
        for meta in items:
            if meta.get('type') != 'file' or not re.fullmatch(r'[0-9a-fA-F]{64}', meta.get('sha256') or ''):
                continue
            value = dict(meta)
            weight = len(json.dumps(value, ensure_ascii=False).encode('utf-8')) + 256
            self.memory_cache.put(('source', epoch, job_id, meta['path']), value, weight, ttl=60)

    def process(self, job_id, entry, control):
        with self.store.session():
            while True:
                try:
                    return self._process(job_id, entry, control)
                except StorageRetry:
                    control.check()

    def _process(self, job_id, entry, control):
        entry = dict(entry)
        job = self.store.job(job_id)
        path = entry["path"]
        mode = job["settings"]["mode"]
        if job['settings'].get('missing_policy') == 'both':
            mode = 'both'
        if job["settings"].get("operation") == "missing" or job["generation"] > 0:
            mode = "both" if job['settings'].get('missing_policy') == 'both' else "fallback"
        errors = []
        other_errors = []
        completed, reused = [], []
        original_status = preview_status = "not_requested"
        fallback = False
        finalized = False
        final_error = False
        try:
            control.check()
            if (entry.get('error') or entry.get('original_attempts', 0) or entry.get('preview_attempts', 0)
                    or entry.get('original_status', 'not_requested') != 'not_requested'
                    or entry.get('preview_status', 'not_requested') != 'not_requested' or entry.get('fallback', 0)):
                self.store.update_entry(job_id, path, error="", original_attempts=0, preview_attempts=0, original_status="not_requested", preview_status="not_requested", fallback=0)
            control.progress(path, 0, None, "Проверка метаданных")
            cached_meta = None
            if job['settings'].get('economy_mode') and mode != 'previews':
                cached_meta = self.memory_cache.get(('source', self.api.credentials.epoch, job_id, path))
            meta = dict(cached_meta) if cached_meta else self.api.metadata(path, job["settings"], control.check, limit=1)
            validate_resource(meta)
            if meta["type"] != "file":
                raise APIError("Исходный файл теперь является папкой")
            safe = {k: meta[k] for k in ("path", "name", "type", "size", "md5", "sha256", "modified", "created", "resource_id", "mime_type", "media_type") if k in meta}
            from .selection_rules import taken
            source_taken = taken(meta)
            if isinstance(source_taken, str) and len(source_taken) <= 100:
                safe['taken'] = source_taken
            safe["file_type"] = file_type(meta)
            safe["has_preview"] = preview_type(meta) and bool(meta.get("preview"))
            encoded_meta = json.dumps(safe, ensure_ascii=False)
            selected = selected_type(meta, job['settings'])
            # A fresh scan has already stored this exact metadata and removed
            # stale artifacts. Do not commit an identical auxiliary update.
            if encoded_meta != entry['meta'] or not selected:
                with self.store.db() as db:
                    db.execute("UPDATE entries SET meta=? WHERE job=? AND path=?", (encoded_meta, job_id, path))
                    db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND fingerprint!=?", (job_id, path, fingerprint(meta)))
                    if not preview_type(meta):
                        db.execute("DELETE FROM artifacts WHERE job=? AND path=? AND type='preview'", (job_id, path))
                    if not selected:
                        db.execute("DELETE FROM artifacts WHERE job=? AND path=?", (job_id, path))
                        db.execute("UPDATE entries SET status='filtered',error='' WHERE job=? AND path=?", (job_id, path))
            if not selected:
                return
            if not preview_type(meta):
                mode = "originals"
            kinds = ["preview"] if mode == "previews" else ["original"] if mode == "originals" else ["original", "preview"]
            for kind in kinds:
                control.check()
                if kind == "preview" and "original" in completed and mode == "fallback":
                    preview_status = "not_needed"
                    self.remove_preview(job, path, control)
                    break
                if kind == "preview" and mode == "fallback":
                    fallback = True
                try:
                    if cached_meta and kind == 'preview':
                        fresh = self.api.metadata(path, job['settings'], control.check, limit=1)
                        validate_resource(fresh)
                        if fingerprint(fresh) != fingerprint(meta):
                            raise APIError('Файл на Яндекс.Диске изменился после обхода. Повторите загрузку этого файла, чтобы получить новую версию.')
                        meta = fresh
                    from .duplicates import content_lock, reuse, compact
                    with content_lock(job,kind,meta,control):
                        was_reused = self.reusable(job, entry, kind, meta, control)
                        if not was_reused and job['settings'].get('deduplicate'):
                            was_reused = reuse(self,job,entry,kind,meta,control)
                        if was_reused:
                            reused.append(kind)
                            outcome = "existing_verified"
                        else:
                            self.transfer(job, entry, kind, meta, control)
                            outcome = "downloaded_verified"
                        if job['settings'].get('deduplicate'):
                            compact(self,job,entry,kind,meta,control)
                    completed.append(kind)
                    label = "Оригинал" if kind == "original" else "Превью"
                    result = (" уже скачан, проверка пройдена" if kind == "original" else " уже скачано, проверка пройдена") if was_reused else (" скачан и проверен" if kind == "original" else " скачано и проверено")
                    control.event("cache" if was_reused else kind, label + result + (" · сохранено вместо недоступного оригинала" if fallback else ""), path, "success")
                except (Suspended, StorageRetry):
                    raise
                except Exception as exc:
                    if control.storage_error(exc):
                        raise StorageRetry() from exc
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
                final_error = True
                control.event("errors", "; ".join(errors if not completed else other_errors), path, "error")
            self.store.update_entry(job_id, path, status=status, error="; ".join(errors if not completed else other_errors), original_status=original_status, preview_status=preview_status, fallback=int(fallback))
            finalized = True
        except StorageRetry:
            # The same future retries in process(). Keep ownership until it
            # finishes, otherwise the scheduler can submit this file twice.
            self.store.update_entry(job_id, path, status='running')
            raise
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
            final_error = True
            control.event("errors", "Ошибка обработки файла: " + safe_error(exc), path, "error")
        except Exception as exc:
            if control.storage_error(exc):
                self.store.update_entry(job_id,path,status='running')
                raise StorageRetry() from exc
            self.store.update_entry(job_id, path, status="error", error=safe_error(exc), original_status=original_status, preview_status=preview_status)
            finalized = True
            final_error = True
            control.event("errors", "Ошибка обработки файла: " + safe_error(exc), path, "error")
        finally:
            with control.lock:
                control.active.pop(path, None)
                if finalized:
                    control.session_errors += int(final_error)
                    control.remaining_time.result(bool(completed), reused=bool(completed) and len(reused) == len(completed) and not errors, fallback=fallback)

    def transfer(self, job, entry, kind, meta, control):
        control.check()
        while control.space_draining:
            control.check()
            time.sleep(.1)
        identity = threading.get_ident()
        with control.space_lock:
            control.transfer_sizes[identity] = None
        try:
            return self._transfer(job, entry, kind, meta, control)
        finally:
            with control.space_lock:
                control.transfer_sizes.pop(identity,None)
                if control.space_draining and not control.transfer_sizes:
                    control.next_space_check = 0

    def _transfer(self, job, entry, kind, meta, control):
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
            with control.space_lock:
                control.transfer_sizes[threading.get_ident()] = max(0,meta['size']-size) if kind=='original' and isinstance(meta.get('size'),int) else None
            etag, total = row["etag"], row["total"]
            can_resume = persistent and (kind == "original" and (meta.get("sha256") or meta.get("md5")) or bool(etag))
            if size and not can_resume:
                with open(temp, "wb"):
                    pass
                size = offset = 0
                sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
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
                            sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
                            total = expected
                        else:
                            raise APIError(f"Ошибка загрузки HTTP {response.status_code}", response.status_code)
                        if kind == "original" and meta.get("size") is not None and total is not None and total != meta["size"]:
                            keep = False
                            raise APIError("Размер ответа оригинала не совпадает с метаданными")
                        etag = new_etag
                        with control.space_lock:
                            control.transfer_sizes[threading.get_ident()] = max(0,total-size) if total is not None else None
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
                                control.before_write(len(chunk))
                                deadline.check()
                                with control.measure('write'):
                                    stream.write(chunk)
                                with control.measure('hash'):
                                    sha.update(chunk)
                                    md5.update(chunk)
                                size += len(chunk)
                                with control.space_lock:
                                    control.transfer_sizes[threading.get_ident()] = max(0,total-size) if total is not None else None
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
                            with control.measure('flush'):
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
                        with Image.open(temp, formats=PREVIEW_FORMATS) as image:
                            fmt = image.format
                            if image.width * image.height > 16_000_000:
                                keep = False
                                raise APIError("Превью превышает безопасный размер изображения")
                            image.verify()
                        with Image.open(temp, formats=PREVIEW_FORMATS) as image:
                            image.load()
                        extensions = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif", "AVIF": "avif"}
                        if fmt not in extensions:
                            keep = False
                            raise APIError("Неизвестный формат изображения превью")
                        proposed = entry["local"] + ".preview." + extensions[fmt]
                    # Synthetic names cannot collide with a real disk:/ path.
                    identity = entry["path"] if kind == "original" else "@preview:" + entry["path"]
                    relative = self.allocate_publication_name(job["id"], identity, proposed)
                    base_relative = relative
                    target = safe_join(job["root"], relative)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    counter = 0
                    while os.path.exists(target):
                        counter += 1
                        parent, _, name = base_relative.rpartition("/")
                        name = suffix(name, digest(fingerprint(meta) + kind + str(counter)))
                        version_identity = "@version:" + kind + ":" + entry['path'] + ":" + fingerprint(meta) + ":" + str(counter)
                        relative = self.allocate_publication_name(job["id"], version_identity, (parent + "/" if parent else "") + name)
                        target = safe_join(job["root"], relative)
                    deadline.check()
                    with self.store.db() as db:
                        db.execute('INSERT OR REPLACE INTO pending_publications VALUES(?,?,?,?,?,?,?,?,?)', (job['id'], entry['path'], kind, Path(temp).name, relative, size, sha.hexdigest(), fingerprint(meta), settings['preview_size'] if kind == 'preview' else None))
                    publish(temp, target)
                    self.partials.discard(job, entry["path"], kind, remove=False)
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (job["id"], entry["path"], kind, relative, size, sha.hexdigest(), fingerprint(meta), settings["preview_size"] if kind == "preview" else None))
                        db.execute('DELETE FROM pending_publications WHERE job=? AND path=? AND type=?', (job['id'], entry['path'], kind))
                    with control.lock:
                        control.session_saved_bytes += size
                        if kind == 'original':control.session_originals += 1
                        else:control.session_previews += 1
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
        control.local_only = True
        job = self.store.job(job_id)
        summary = self.store.summary(job_id)
        final_phase = "stopped" if manual and (not job["scan_complete"] or summary["cancelled"] or summary["processed"] < summary["files"] or job["phase"] in ("stopped", "interrupted", "failed")) else ("completed_with_errors" if summary["errors"] or summary["scan_errors"] else "completed")
        if control.selected_paths and (not job["scan_complete"] or summary["cancelled"] or summary["processed"] < summary["files"]):
            final_phase = "stopped"
        self.store.update_job(job_id, ended=job["ended"] or now())
        self.phase(job_id, control, "preparing")
        control.progress(job["settings"]["paths"][0], 0, None, "Создание отчётов", unit="objects")
        control.check()
        temp = None
        pending_zip, last_flush = 0, time.monotonic()
        try:
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
                self.store.update_job(job_id, zip_total=total, zip_done=0)
                self.phase(job_id, control, "packing")
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                for row in self.store.rows("SELECT local FROM entries WHERE job=? AND kind='dir' AND local!='' ORDER BY local", (job_id,)):
                    control.check()
                    relative = row["local"]
                    if os.path.isdir(safe_join(job["root"], relative)):
                        output.writestr(relative + "/", b"")
                report_folder = job["service"] + "/reports"
                output.writestr(report_folder + "/", b"")
                artifacts = self.store.rows("SELECT DISTINCT a.local,a.size,a.sha256 FROM artifacts a JOIN entries e ON e.job=a.job AND e.path=a.path WHERE a.job=? ORDER BY a.local", (job_id,))
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
                            control.before_write(len(chunk))
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
            with open(temp,'r+b') as stream:
                os.fsync(stream.fileno())
            control.check()
            publish(temp, archive)
            self.store.update_job(job_id, phase=final_phase, zip_path=archive_rel, zip_bytes=os.path.getsize(archive), generation=generation)
            control.event("archive", "Отчёты и ZIP сохранены: " + archive_rel, level="success")
        finally:
            try:
                if pending_zip:
                    with self.store.db() as db:
                        db.execute("UPDATE jobs SET zip_done=zip_done+? WHERE id=?", (pending_zip, job_id))
            finally:
                try:
                    if temp and os.path.exists(temp):
                        os.unlink(temp)
                finally:
                    if temp:
                        forget_temporary(self.store,job['root'],temp)
                        with control.lock:
                            control.temporary.discard(temp)
                            control.temporary_sizes.pop(temp, None)
