import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .paths import PathError, publish, replace_owned, reserve_output, safe_join, temporary_file
from .service_outputs import location, finish


GROUPS = {"system": "Задание", "scan": "Обход диска", "original": "Оригиналы", "preview": "Превью", "cache": "Уже скачанное", "errors": "Ошибки и повторы", "archive": "Отчёты и ZIP"}
PHASES = {"new": "Новое задание", "scanning": "Обход диска", "preparing": "Подготовка результатов", "ready": "Готово к загрузке", "downloading": "Загрузка", "packing": "Создание ZIP", "paused": "Пауза", "stopping": "Остановка", "stopped": "Остановлено", "failed": "Ошибка", "interrupted": "Прервано", "completed": "Завершено", "completed_with_errors": "Завершено с замечаниями"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


class Journal:
    def __init__(self, engine, job_id, interval=300):
        self.engine = engine
        self.store = engine.store
        self.job_id = job_id
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO journal_jobs VALUES(?,?)',(job_id,self.store.job(job_id)['root']))
        self.interval = interval
        self.stopped = threading.Event()
        self.write_lock = threading.Lock()
        self.secret_lock = threading.Lock()
        self.secrets = set()
        self.error = ""
        self.saved = None
        self.thread = threading.Thread(target=self.watch, name="disk-journal", daemon=True)

    def clean(self, value):
        credentials = self.engine.api.credentials
        with credentials.lock:
            token = credentials.token
        with self.secret_lock:
            if token:
                self.secrets.add(token)
            result = str(value or "")
            for secret in sorted(self.secrets, key=len, reverse=True):
                result = result.replace(secret, "[ключ скрыт]")
        result = re.sub(r"https?://[^\s<>\"']+", "[ссылка скрыта]", result, flags=re.I)
        result = re.sub(r"\b(Bearer|OAuth)\s+\S+", r"\1 [ключ скрыт]", result, flags=re.I)
        result = re.sub(r"\b(access_token|refresh_token|client_secret)\s*[=:]\s*[^\s&,;]+", r"\1=[ключ скрыт]", result, flags=re.I)
        return result

    def event(self, group, message, path="", level="info"):
        try:
            with self.store.db() as db:
                db.execute("INSERT INTO journal_events(job,time,category,level,message,path) VALUES(?,?,?,?,?,?)", (self.job_id, datetime.now(timezone.utc).isoformat(), group, level, self.clean(message), self.clean(path)))
        except sqlite3.Error:
            self.error = "Не удалось записать событие журнала"

    def start(self, operation):
        names = {"scan": "Начат обход выбранных источников", "download": "Начата загрузка или дозагрузка", "pack": "Начата повторная сборка ZIP", 'manifest':'Обновляем опись загрузки', 'journal':'Обновляем HTML-журнал'}
        self.event("system", names.get(operation, operation))
        self.flush()
        self.thread.start()

    def watch(self):
        due = time.monotonic() + self.interval
        while not self.stopped.wait(max(0, due - time.monotonic())):
            self.flush()
            try:
                from .reports import write_reports, REPORT_LOCK
                from .import_state import save_import
                def checkpoint():
                    if self.stopped.is_set():
                        raise ValueError('Периодическое сохранение остановлено')
                with REPORT_LOCK:
                    control=self.engine.controls.get(self.job_id)
                    if not self.stopped.is_set() and getattr(control,'phase',None)!='packing':
                        write_reports(self.store, self.job_id, checkpoint)
                        save_import(self.store, self.job_id, checkpoint)
            except (OSError, sqlite3.Error, ValueError):
                self.error = 'Не удалось обновить отчёты или опись'
            due += self.interval
            if due <= time.monotonic():
                due = time.monotonic() + self.interval

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            # Exports check stopped between records and before publication.
            # Never orphan a writer that could overwrite a newer manifest.
            self.thread.join()
        self.flush(final=True)
        with self.secret_lock:
            self.secrets.clear()

    def owned(self, job, relative, marker):
        with self.store.db() as db:
            registered = db.execute("SELECT 1 FROM names WHERE job=? AND identity=? AND local=?", (job["id"], "@output:" + relative, relative)).fetchone()
        if not registered:
            return False
        filename = safe_join(job["root"], relative)
        if not os.path.lexists(filename):
            return True
        try:
            with open(filename, "r", encoding="utf-8") as stream:
                return stream.readline(256).rstrip("\n") == marker
        except (UnicodeError, IsADirectoryError):
            return False

    def flush(self, final=False):
        temp = None
        started = time.monotonic()
        def checkpoint():
            control = self.engine.controls.get(self.job_id)
            worker_stopped = control and control.stopped and threading.current_thread() is self.engine.threads.get(self.job_id)
            if (not final and ((threading.current_thread() is self.thread and self.stopped.is_set()) or worker_stopped)) or (final and control and control.stopped and time.monotonic()-started>3):
                raise ValueError('HTML-журнал обновится позднее; события сохранены в истории')
        try:
            with self.write_lock:
                job = self.store.job(self.job_id)
                settings = job["settings"]
                key = settings.get("journal_key") or uuid.uuid4().hex
                marker = '<!doctype html><html lang="ru" data-journal="' + self.job_id + ":" + key + '">'
                relative, replacing = location(self.store, job, 'journal', 'log.html',checkpoint)
                if settings.get("journal_local") != relative or settings.get("journal_key") != key:
                    with self.store.db() as db:
                        current = json.loads(db.execute("SELECT settings FROM jobs WHERE id=?", (self.job_id,)).fetchone()[0])
                        current.update(journal_local=relative, journal_key=key)
                        db.execute("UPDATE jobs SET settings=? WHERE id=?", (json.dumps(current, ensure_ascii=False), self.job_id))
                # Journal needs database totals, never a UI snapshot that may
                # acquire engine locks or stat thousands of local files.
                status = self.store.summary(self.job_id)
                updated = datetime.now(timezone.utc).isoformat()
                summary = {"updated": updated, "id": self.job_id[:12], "phase": PHASES.get(status["phase"], status["phase"]), "files": status["files"], "processed": status["processed"], "saved_bytes": status["saved_bytes"], "root": self.clean(job["root"]), "groups": GROUPS}
                with self.store.db() as db:
                    ceiling = db.execute("SELECT COALESCE(MAX(id),0) FROM journal_events").fetchone()[0]
                template = Path(__file__).with_name("templates").joinpath("journal.html").read_text(encoding="utf-8")
                before, after = template.split("__JOURNAL_DATA__")
                temp = temporary_file(job["root"], self.store, 'journal')
                with open(temp, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(marker + "\n" + before + '{"summary":' + encoded(summary) + ',"events":[')
                    first = True
                    for row in self.store.rows("SELECT e.time,e.category,e.level,e.message,e.path FROM journal_events e LEFT JOIN jobs j ON j.id=e.job LEFT JOIN journal_jobs jj ON jj.job=e.job WHERE COALESCE(j.root,jj.root)=? AND e.id<=? ORDER BY e.id", (job['root'], ceiling)):
                        checkpoint()
                        if not first:
                            stream.write(",\n")
                        stream.write(encoded(dict(row)))
                        first = False
                    stream.write("]}" + after)
                    stream.flush()
                    os.fsync(stream.fileno())
                finish(self.store, job, 'journal', relative, temp, replacing,checkpoint)
                self.saved = updated
                self.error = ""
        except (OSError, sqlite3.Error, ValueError, UnicodeError):
            self.error = "Не удалось сохранить HTML-журнал. Проверьте свободное место и доступ к папке."
        finally:
            if temp and os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    self.error = "Не удалось удалить временный файл журнала"
