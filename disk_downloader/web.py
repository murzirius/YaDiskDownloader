import io
import os
import secrets
import threading
import json
import sqlite3
import shutil
import tempfile
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, session
from PIL import Image
from .image_security import PREVIEW_FORMATS
from werkzeug.exceptions import HTTPException

from .api import APIError, Credentials, Deadline, DiskAPI, SIZES, Suspended, validate_resource
from .engine import Engine, safe_error
from .paths import remote_path, safe_join
from .store import Store
from .file_types import preview_type
from .updates import Updates
from .diagnostics import bundle, write_bundle
from . import __version__
from .inventory import folders, change_summary, LABELS
from .issues import draft
from .runtime import state_folder, destination_folder, frozen
from .backups import Backups
from .management import Management
from .maintenance import Maintenance
from .duplicate_review import DuplicateReview
from .recovery import Recovery
from .updater import Installer
from .service_outputs import root_key, digest_file
from .limits import normalize_saved_workers
from .folder_dialog import FolderPicker
from .import_state import find_import
from .request_errors import RequestErrors
from .platform_actions import open_path
from .insights import DestinationChecks, download_plan, files as job_files, problems as job_problems


def create_app(state_dir=None, api=None, port=8766):
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(MAX_CONTENT_LENGTH=1024 * 1024, SESSION_COOKIE_NAME=f"disk_downloader_{port}", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SEND_FILE_MAX_AGE_DEFAULT=0)
    state = Path(state_dir or state_folder())
    store = Store(state / "jobs.sqlite3")
    from .single_instance import state_key
    state_identity = state_key(state)
    recovery = Recovery(store)
    credentials = api.credentials if api else Credentials()
    api = api or DiskAPI(credentials)
    engine = Engine(store, api)
    engine.recovery = recovery
    recovery.start()
    updates = Updates(store)
    management, maintenance, backups = Management(engine), Maintenance(engine), Backups(store)
    duplicate_review=DuplicateReview(engine)
    app.extensions['duplicate_review']=duplicate_review
    installer = Installer(engine,updates,backups,port)
    app.extensions['installer'] = installer
    app.extensions.update(engine=engine, store=store, disk_api=api, updates=updates, management=management, maintenance=maintenance, backups=backups, recovered=0, recovery=recovery)
    thumbnail_slots = threading.BoundedSemaphore(3)
    browse_slots = threading.BoundedSemaphore(2)
    local_folder_slots = threading.BoundedSemaphore(2)
    folder_picker = FolderPicker()
    app.extensions['folder_picker'] = folder_picker
    request_errors = RequestErrors(state)
    engine.request_errors = request_errors
    destination_checks = DestinationChecks(store)
    app.extensions['destination_checks'] = destination_checks
    from .folder_stats import FolderStats
    folder_stats = FolderStats(api)
    app.extensions['folder_stats'] = folder_stats
    from .doctor import Doctor
    doctor = Doctor(engine)
    app.extensions['doctor'] = doctor
    auth_lock = threading.Lock()
    instance_id = secrets.token_hex(8)

    def auth_status():
        return credentials.status() | {"instance_id": instance_id, 'recovery':recovery.snapshot()}

    @app.post('/api/recovery')
    def retry_recovery():
        with engine.lock:
            if any(t.is_alive() for t in engine.threads.values()) or engine.maintenance_busy:
                raise ValueError('Остановите текущую работу перед восстановлением')
            recovery.start()
        return jsonify(recovery.snapshot())
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.before_request
    def security():
        if request.host not in hosts or request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403, "Разрешены только локальные обращения")
        origin = request.headers.get("Origin")
        if origin and origin not in origins:
            abort(403, "Недопустимый Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403, "Межсайтовый запрос запрещён")
        if request.path.startswith("/api/"):
            if request.path == '/api/backups/import':
                request.max_content_length = 1024 * 1024 * 1024
            csrf = session.get("csrf")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not csrf or not supplied.isascii() or not secrets.compare_digest(supplied, csrf):
                abort(403, "Эта вкладка открыта до перезапуска приложения или потеряла связь с ним. Обновите страницу (F5). Скачанные файлы и история сохранены.")
        if request.method == "POST" and origin not in origins:
            abort(403, "Для управляющего запроса требуется локальный Origin")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        request_id = secrets.token_hex(4)
        context = {"local_folders":"Открытие папки на ПК", "queue_status":"Чтение очереди заданий", "queue_action":"Управление очередью заданий", "folder":"Выбор папки Windows", "folder_status":"Выбор папки Windows", "connect": "Проверка ключа доступа в Яндекс.Диске", "browse": "Получение списка файлов Яндекс.Диска", "job_status": "Обновление состояния задания", "status": "Чтение истории заданий", "job_action": "Управление заданием"}.get(request.endpoint, "Запрос к приложению на этом ПК")
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description, operation=context, code="session_expired" if exc.code == 403 and "вкладка" in exc.description else "http_error", request_id=request_id), exc.code
        if isinstance(exc, (ValueError, APIError, Suspended, OSError)):
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else safe_error(exc), operation=context, code="yandex_error" if isinstance(exc, APIError) else "action_error", request_id=request_id, **auth_status()), 400
        app.logger.error("Request %s endpoint=%s exception=%s sqlite=%s", request_id, request.endpoint, type(exc).__name__, getattr(exc, "sqlite_errorname", ""))
        request_errors.record(request_id, request.endpoint, exc)
        message = safe_error(exc) if isinstance(exc, sqlite3.Error) else "В приложении на этом ПК произошла ошибка. Прогресс уже сохранённых файлов остаётся в истории. Откройте «Сообщить об ошибке» и приложите диагностику."
        return jsonify(error=message, operation=context, code="local_database_error" if isinstance(exc, sqlite3.Error) else "internal_error", request_id=request_id), 500

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Ожидается JSON-объект")
        return data

    @app.get("/")
    def index():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        updates.start()
        return render_template("index.html", csrf=session["csrf"], version=__version__, default_destination=str(destination_folder()))

    @app.get("/api/runtime")
    def runtime():
        tray = app.extensions.get("tray")
        from .sqlite_support import safe_wal
        import sqlite3
        from . import __build__
        from .desktop import status as desktop_status
        from .single_instance import state_key
        return jsonify(application='YaDiskDownloader', state_key=state_identity, sqlite=sqlite3.sqlite_version, safe_sqlite_wal=safe_wal(), version=__version__, build=__build__, frozen=frozen(), tray=tray.status() if tray else {"visible": False, "error": ""}, desktop=desktop_status(), can_exit=bool(app.extensions.get("quit_app")), migration=app.extensions.get("migration", {"jobs": 0, "failed_sources": 0}))

    @app.post('/api/activate')
    def activate_instance():
        activate = app.extensions.get('activate_window')
        if not activate:raise ValueError('Приложение на ПК ещё запускается. Подождите немного и повторите открытие.')
        activate()
        return jsonify(ok=True)

    @app.post("/api/exit")
    def exit_app():
        quit_app = app.extensions.get("quit_app")
        if not quit_app:
            raise ValueError("Завершение недоступно в этом режиме запуска")
        threading.Timer(0.3, quit_app).start()
        return jsonify(ok=True)

    @app.post("/api/issue-draft")
    def issue_draft():
        return jsonify(draft(payload(), engine, updates.settings()["repository"]))

    @app.get("/api/jobs/<job_id>/folders")
    def folder_sizes(job_id):
        offset = request.args.get("offset", "0")
        if not offset.isdigit() or len(offset) > 8:
            raise ValueError("Некорректное смещение")
        return jsonify(folders(store, job_id, remote_path(request.args.get("path", "disk:/")), int(offset)))

    @app.get("/api/jobs/<job_id>/changes")
    def changes(job_id):
        summary = change_summary(store, job_id)
        status, offset = request.args.get("status", "all"), request.args.get("offset", "0")
        if status not in (*LABELS, "all") or not offset.isdigit() or len(offset) > 8:
            raise ValueError("Некорректные параметры изменений")
        rows = list(store.rows("SELECT path,status,before_meta,after_meta FROM changes WHERE job=? AND (?='all' OR status=?) ORDER BY path LIMIT 101 OFFSET ?", (job_id, status, status, int(offset))))
        items = [{"path": row["path"], "status": row["status"], "before_size": json.loads(row["before_meta"]).get("size") if row["before_meta"] else None, "after_size": json.loads(row["after_meta"]).get("size") if row["after_meta"] else None} for row in rows[:100]]
        return jsonify(summary=summary, items=items, has_more=len(rows) > 100, next_offset=int(offset) + len(items))

    @app.post("/api/jobs/<job_id>/open-changes")
    def open_changes(job_id):
        job = store.job(job_id)
        relative = job["settings"].get("changes_local")
        if not relative:
            raise ValueError("Отчёт появится после загрузки или сборки ZIP")
        filename = safe_join(job["root"], relative)
        with store.db() as db:
            owned = db.execute("SELECT sha256 FROM service_outputs WHERE root=? AND kind='changes' AND local=?",(root_key(job['root']),relative)).fetchone()
        if not owned or os.stat(filename).st_nlink != 1 or digest_file(filename) != owned['sha256']:
            raise ValueError('Файл отчёта заменён другим файлом')
        open_path(filename)
        return jsonify(path=str(Path(job["root"]) / relative))

    @app.get('/api/doctor')
    def doctor_status():
        if doctor.snapshot()['checked_at'] is None and not doctor.snapshot()['busy']:
            doctor.start()
        return jsonify(doctor.snapshot())

    @app.post('/api/doctor')
    def doctor_start():
        return jsonify(doctor.start())

    @app.post('/api/jobs/<job_id>/refresh-source')
    def refresh_source(job_id):
        previous = store.job(job_id)
        data = dict(previous['settings'], operation='missing', destination=previous['root'])
        return jsonify(id=engine.create(data))

    @app.post('/api/jobs/<job_id>/limits')
    def change_live_limits(job_id):
        return jsonify(engine.live_limits(job_id, payload()))

    @app.get("/api/updates")
    def update_status():
        return jsonify(updates.snapshot())

    @app.post("/api/updates")
    def check_updates():
        data = payload()
        if "repository" in data or "automatic" in data:
            updates.save(data)
        updates.start(force=True)
        return jsonify(updates.snapshot())

    @app.get('/api/update-install')
    def install_status(): return jsonify(installer.snapshot())

    @app.post('/api/update-install')
    def update_install():
        action = payload().get('action')
        if action == 'download': return jsonify(installer.download())
        if action == 'install': return jsonify(installer.install(app.extensions.get('quit_app')))
        raise ValueError('Неизвестная команда обновления')

    @app.get("/api/profiles")
    def profiles():
        items = []
        for row in store.rows("SELECT * FROM profiles ORDER BY name"):
            try:
                value = json.loads(row["settings"])
                if not isinstance(value, dict):
                    raise ValueError()
                normalize_saved_workers(value)
                empty_sources = not value.get("whole_disk") and not value.get("paths")
                validated = engine.validate_settings(value | {"whole_disk": True} if empty_sources else value)
                if empty_sources:
                    validated.update(paths=[], whole_disk=False)
                items.append({"name": row["name"], "settings": validated})
            except (ValueError, TypeError, OSError):
                items.append({"name": row["name"], "settings": None, "error": "Профиль повреждён. Создайте его заново."})
        return jsonify(items=items)

    @app.post("/api/profiles")
    def save_profile():
        data = payload()
        name = data.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("Название профиля: от 1 до 80 символов")
        name = name.strip()
        if data.get("delete"):
            with store.db() as db:
                db.execute("DELETE FROM profiles WHERE name=?", (name,))
            return jsonify(ok=True)
        value = data.get("settings")
        if not isinstance(value, dict):
            raise ValueError("Некорректные настройки профиля")
        empty_sources = not value.get("whole_disk") and not value.get("paths")
        validated = engine.validate_settings(value | {"whole_disk": True} if empty_sources else value)
        if empty_sources:
            validated.update(paths=[], whole_disk=False)
        with store.db() as db:
            if db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] >= 100 and not db.execute("SELECT 1 FROM profiles WHERE name=?", (name,)).fetchone():
                raise ValueError("Можно сохранить до 100 профилей")
            db.execute("INSERT OR REPLACE INTO profiles VALUES(?,?)", (name, json.dumps(validated, ensure_ascii=False)))
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/diagnostics-preview")
    def preview_diagnostics(job_id):
        return jsonify(files=bundle(engine, job_id))

    @app.post("/api/jobs/<job_id>/diagnostics")
    def diagnostics(job_id):
        local = write_bundle(engine, job_id)
        job = store.job(job_id)
        open_path(safe_join(job["root"], ""), folder=True)
        return jsonify(local=local)

    @app.get("/api/status")
    def status():
        jobs = list(store.rows("SELECT id,title,phase,started,root FROM jobs WHERE archived=0 ORDER BY started DESC LIMIT 30"))
        active = None
        for job_id, thread in tuple(engine.threads.items()):
            if thread.is_alive():
                job = store.job(job_id)
                active = {key: job[key] for key in ('id', 'phase', 'scan_complete')}
                break
        return jsonify(jobs=jobs, active_job=active, **auth_status())

    @app.get('/api/history')
    def history():
        offset = request.args.get('offset','0')
        if not offset.isascii() or not offset.isdigit() or len(offset)>7:
            raise ValueError('Некорректная страница истории')
        return jsonify(management.history(request.args.get('query',''),request.args.get('phase','all'),request.args.get('archived')=='1',int(offset)))

    @app.post('/api/jobs/<job_id>/history')
    def change_history(job_id):
        return jsonify(management.history_action(job_id,payload()))

    @app.get('/api/queue')
    def queue_status(): return jsonify(management.queue_snapshot())

    @app.post('/api/queue')
    def queue_action(): return jsonify(management.queue_action(payload()))

    @app.get('/api/verification')
    def verification_status():
        offset = request.args.get('offset','0')
        if not offset.isascii() or not offset.isdigit() or len(offset)>8:
            raise ValueError('Некорректная страница проверки')
        return jsonify(maintenance.snapshot(int(offset)))

    @app.post('/api/verification/repair')
    def verification_repair(): return jsonify(settings=maintenance.repair_settings())

    @app.post('/api/verification')
    def verify_folder():
        data=payload()
        if data.get('action')=='cancel':
            maintenance.cancel.set()
            return jsonify(ok=True)
        return jsonify(maintenance.start(data))

    @app.get('/api/jobs/<job_id>/duplicates')
    def duplicate_list(job_id):
        offset=request.args.get('offset','0')
        if not offset.isascii() or not offset.isdigit() or len(offset)>8:raise ValueError('Некорректная страница дубликатов.')
        return jsonify(duplicate_review.snapshot(job_id,int(offset)))

    @app.post('/api/jobs/<job_id>/duplicates')
    def duplicate_cleanup(job_id):
        data=payload()
        if data.get('action')=='cancel':
            store.job(job_id)
            with duplicate_review.lock:
                if duplicate_review.result['job']!=job_id:raise ValueError('В этом задании очистка не запущена.')
            duplicate_review.cancel()
            return jsonify(duplicate_review.snapshot(job_id))
        return jsonify(duplicate_review.start(job_id,data))

    @app.post('/api/cleanup-preview')
    def cleanup_preview(): return jsonify(maintenance.cleanup_preview(payload().get('root')))

    @app.post('/api/cleanup')
    def cleanup_files(): return jsonify(maintenance.cleanup(payload()))

    @app.get('/api/backups')
    def list_backups(): return jsonify(items=backups.list())

    @app.post('/api/backups')
    def backup_create(): return jsonify(name=backups.create())

    @app.get('/api/backups/<name>/download')
    def backup_download(name): return send_file(backups.path(name),as_attachment=True,download_name=name)

    @app.post('/api/backups/import')
    def backup_import():
        file=request.files.get('file')
        if not file: raise ValueError('Выберите файл резервной копии')
        return jsonify(name=backups.import_file(file.stream))

    @app.post('/api/backups/restore')
    def backup_restore():
        data=payload()
        if data.get('confirm') is not True: raise ValueError('Подтвердите восстановление истории и профилей')
        with engine.lock:
            engine.ensure_idle()
            safety=backups.restore(data.get('name'))
            credentials.clear()
            engine.controls.clear(); engine.threads.clear(); management.armed=False
            engine.status_cache.invalidate()
            with maintenance.lock:
                maintenance.result={'busy':False,'processed':0,'total':0,'items':[],'counts':{},'complete':False}
        return jsonify(ok=True,safety_copy=safety)

    @app.get('/api/notifications')
    def notification_settings():
        with store.db() as db: row=db.execute("SELECT value FROM preferences WHERE key='notifications'").fetchone()
        return jsonify(enabled=not row or row[0]=='true')

    @app.post('/api/notifications')
    def save_notifications():
        enabled=payload().get('enabled')
        if not isinstance(enabled,bool): raise ValueError('Некорректная настройка уведомлений')
        with store.db() as db: db.execute("INSERT OR REPLACE INTO preferences VALUES('notifications',?)",('true' if enabled else 'false',))
        return jsonify(enabled=enabled)

    @app.post("/api/connect")
    def connect():
        data = payload()
        token = data.get("token")
        candidate = Credentials()
        candidate.set(token)  # Validate syntax before starting a network request.
        candidate.clear()
        if not auth_lock.acquire(blocking=False):
            raise ValueError("Ключ уже проверяется. Дождитесь результата подключения; повторно нажимать кнопку не нужно.")
        try:
            with engine.lock:
                engine.ensure_idle()
                with credentials.lock:
                    epoch = credentials.epoch
            if hasattr(api, "verify_token"):
                api.verify_token(token)
            else:
                api.metadata("disk:/", {"attempts": 1, "timeout": 15}, limit=1)
            with engine.lock:
                engine.ensure_idle()
                credentials.set(token, expected_epoch=epoch)
            return jsonify(**auth_status())
        finally:
            token = None
            auth_lock.release()

    @app.post("/api/disconnect")
    def disconnect():
        engine.disconnect()
        return jsonify(**auth_status())

    @app.get("/api/browse")
    def browse():
        path = remote_path(request.args.get("path", "disk:/"))
        offset = request.args.get("offset", "0")
        sort = request.args.get("sort", "name")
        if not offset.isascii() or not offset.isdigit() or len(offset) > 8 or sort not in ("name", "-name", "size", "-size", "modified", "-modified"):
            raise ValueError("Некорректные параметры списка")
        if not browse_slots.acquire(blocking=False):
            raise ValueError("Список папки уже загружается. Дождитесь ответа; повторно нажимать кнопку не нужно.")
        try:
            data = api.metadata(path, {"preview_size": "S", "attempts": 1, "timeout": 12, "total_timeout": 12}, offset=int(offset), limit=100, sort=sort)
        finally:
            browse_slots.release()
        if data.get('type') != 'dir':
            raise ValueError('Выбранный путь Яндекс.Диска не является папкой')
        embedded = data.get("_embedded", {})
        items = []
        seen = set()
        for item in embedded.get("items", []):
            validate_resource(item)
            from .paths import parent_path
            if item['path']==path or parent_path(item['path'])!=path or item['path'] in seen:
                raise APIError('Яндекс.Диск вернул повторный файл или файл из другой папки')
            seen.add(item['path'])
            items.append({k: item.get(k) for k in ("path", "name", "type", "size", "modified", "mime_type")} | {"has_preview": preview_type(item) and bool(item.get("preview"))})
        cached = folder_stats.snapshot([item['path'] for item in items])['items']
        for item in items:
            if item['type'] == 'dir':
                item['folder_stats'] = cached.get(item['path'])
                item['modified'] = (item['folder_stats'] or {}).get('latest_modified')
                item['size'] = (item['folder_stats'] or {}).get('bytes')
        return jsonify(path=path, items=items, total=embedded.get("total"), offset=int(offset), next_offset=int(offset) + len(items), has_more=bool(items) and (embedded.get("total") is None or int(offset) + len(items) < embedded["total"]))

    @app.get('/api/folder-stats')
    def get_folder_stats():
        return jsonify(folder_stats.snapshot())

    @app.post('/api/folder-stats')
    def start_folder_stats():
        data = payload()
        if data.get('action') == 'cancel':
            folder_stats.cancel.set()
            return jsonify(folder_stats.snapshot())
        return jsonify(folder_stats.start(data.get('paths')))

    @app.get("/api/thumbnail")
    def thumbnail():
        if not thumbnail_slots.acquire(blocking=False):
            return "", 429
        try:
            _, epoch = credentials.snapshot()
            def check():
                credentials.check(epoch)
            with Deadline(15, check, cancelled=lambda: not credentials.connected) as deadline:
                return load_thumbnail(deadline)
        finally:
            thumbnail_slots.release()

    def load_thumbnail(deadline):
        response = connection = None
        try:
            path = remote_path(request.args.get("path", ""))
            size = request.args.get("size", "M")
            if size not in SIZES:
                raise ValueError("Некорректный размер миниатюры")
            response, connection = api.media(path, "preview", {"preview_size": size, "attempts": 1, "timeout": 15, "_single_attempt": True, "_deadline": deadline}, deadline.check)
            data = bytearray()
            for chunk in response.iter_content(65536):
                deadline.check()
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise APIError("Превью слишком большое для миниатюры")
            with Image.open(io.BytesIO(data), formats=PREVIEW_FORMATS) as image:
                if image.width * image.height > 16_000_000:
                    raise APIError("Превью слишком большое для миниатюры")
                image.thumbnail((400, 400))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=85)
            output.seek(0)
            deadline.check()
            return send_file(output, mimetype="image/jpeg")
        finally:
            if response is not None:
                api.close(response, connection)

    @app.post("/api/folder")
    def folder():
        return jsonify(folder_picker.start(payload().get('initial', '')))

    @app.get("/api/folder")
    def folder_status():
        return jsonify(folder_picker.snapshot())

    @app.get('/api/local-folders')
    def local_folders():
        if not local_folder_slots.acquire(blocking=False):
            raise ValueError('Папка ещё открывается. Можно отменить выбор и вставить путь вручную.')
        try:
            from .local_folders import listing
            return jsonify(listing(request.args.get('path', '')))
        finally:
            local_folder_slots.release()

    @app.post("/api/jobs")
    def create_job():
        if any(t.is_alive() for t in engine.threads.values()):
            raise ValueError("Сначала завершите или остановите текущую операцию")
        return jsonify(id=engine.create(payload()))

    @app.post('/api/destination')
    def inspect_destination():
        value=payload().get('path')
        if not isinstance(value,str) or not Path(value).is_absolute() or len(value)>30000 or '\0' in value:
            raise ValueError('Укажите полный путь к папке на ПК, например D:\\Фото')
        path=Path(value).resolve()
        if path.exists() and not path.is_dir():
            raise ValueError('На ПК по этому пути находится файл. Выберите папку.')
        parent=path
        while not parent.exists() and parent!=parent.parent:
            parent=parent.parent
        if not parent.is_dir():
            raise ValueError('Диск на ПК недоступен. Проверьте подключение и выберите папку снова.')
        with tempfile.TemporaryFile(dir=parent, prefix='.yadisk-write-test-') as probe:
            probe.write(b'check'); probe.flush()
        usage=shutil.disk_usage(parent)
        previous=find_import(store,path) if path.is_dir() else None
        return jsonify(path=str(path),exists=path.is_dir(),writable=True,free_bytes=usage.free,total_bytes=usage.total,used_percent=round((usage.total-usage.free)*100/max(1,usage.total),1),previous_import=bool(previous))

    @app.post('/api/destination-check')
    def start_destination_check():
        return jsonify(destination_checks.start(payload().get('path')))

    @app.get('/api/destination-check')
    def destination_check_status():
        return jsonify(destination_checks.snapshot())

    @app.get('/api/jobs/<job_id>/plan')
    def job_plan(job_id):
        return jsonify(download_plan(store,job_id))

    @app.get('/api/jobs/<job_id>/files')
    def search_job_files(job_id):
        offset=request.args.get('offset','0')
        if not offset.isascii() or not offset.isdigit() or len(offset)>8:
            raise ValueError('Некорректная страница списка файлов. Повторите поиск.')
        return jsonify(job_files(store,job_id,request.args.get('q',''),request.args.get('state','all'),int(offset)))

    @app.get('/api/jobs/<job_id>/problems')
    def grouped_job_problems(job_id):
        return jsonify(job_problems(store,job_id))

    @app.post('/api/jobs/<job_id>/open-file-folder')
    def open_file_folder(job_id):
        job=store.job(job_id)
        path=payload().get('path')
        with store.db() as db:
            row=db.execute("SELECT local FROM artifacts WHERE job=? AND path=? ORDER BY type LIMIT 1",(job_id,path)).fetchone()
        if not row:raise ValueError('У этого файла пока нет сохранённой копии. Можно повторить загрузку.')
        filename=safe_join(job['root'],row['local'])
        if not os.path.isfile(filename):raise ValueError('Файл на устройстве не найден. Подключите накопитель или запустите дозагрузку.')
        if os.name=='nt':
            import subprocess
            explorer=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'explorer.exe'
            subprocess.Popen([str(explorer),'/select,'+str(filename)])
        else:
            open_path(Path(filename).parent,folder=True)
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        result=engine.status_cache.snapshot(store.job(job_id)) | auth_status()
        with store.db() as db:
            queued=db.execute('SELECT state FROM job_queue WHERE job=?',(job_id,)).fetchone()
        result['queue_state']=queued[0] if queued else None
        return jsonify(result)

    @app.post('/api/jobs/<job_id>/refresh-usage')
    def refresh_folder_usage(job_id):
        job=store.job(job_id)
        engine.folder_usage.for_job(store, job_id, job['root'],force=True)
        engine.status_cache.invalidate()
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/errors")
    def job_errors(job_id):
        store.job(job_id)
        offset = request.args.get("offset", "0")
        if not offset.isascii() or not offset.isdigit() or len(offset) > 8:
            raise ValueError("Некорректное смещение")
        rows = list(store.rows("SELECT path,status,error,original_status,preview_status,fallback FROM entries WHERE job=? AND kind='file' AND status NOT IN ('pending','running','cancelled') AND (error!='' OR status IN ('error','no_preview')) UNION ALL SELECT path,'scan_error',error,'not_requested','not_requested',0 FROM scan_errors WHERE job=? LIMIT 100 OFFSET ?", (job_id, job_id, int(offset))))
        return jsonify(items=rows, next_offset=int(offset) + len(rows), has_more=len(rows) == 100)

    @app.post("/api/jobs/<job_id>/action")
    def job_action(job_id):
        data = payload()
        if data.get("limits") is not None and not isinstance(data["limits"], dict):
            raise ValueError("Некорректные ограничения")
        limits = dict(data.get("limits") or {})
        if "paths" in data:
            limits["paths"] = data["paths"]
        engine.action(job_id, data.get("action"), limits or None)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/open-output")
    def open_output(job_id):
        job = store.job(job_id)
        if not job["zip_path"] or not os.path.isfile(safe_join(job["root"], job["zip_path"])):
            raise ValueError("ZIP ещё не готов")
        root = safe_join(job["root"], "")
        open_path(root, folder=True)
        return jsonify(path=job["root"], zip_path=job["zip_path"])

    @app.post("/api/jobs/<job_id>/open-folder")
    def open_folder(job_id):
        job = store.job(job_id)
        root = safe_join(job["root"], "")
        if not os.path.isdir(root):
            raise ValueError("Папка с результатами не найдена. Возможно, её перенесли или отключили диск. Подключите диск либо выберите новое расположение в режиме «Дозагрузить недостающее».")
        open_path(root, folder=True)
        return jsonify(path=job["root"])

    @app.post("/api/jobs/<job_id>/open-journal")
    def open_journal(job_id):
        job = store.job(job_id)
        relative = job["settings"].get("journal_local")
        if not relative:
            raise ValueError("Журнал ещё не сохранён")
        filename = safe_join(job["root"], relative)
        with store.db() as db:
            owned = db.execute("SELECT sha256 FROM service_outputs WHERE root=? AND kind='journal' AND local=?",(root_key(job['root']),relative)).fetchone()
        if not owned or os.stat(filename).st_nlink != 1 or digest_file(filename) != owned['sha256']:
            raise ValueError('Файл журнала заменён другим файлом')
        open_path(filename)
        return jsonify(path=str(Path(job["root"]) / relative))

    @app.get("/api/jobs/<job_id>/zip")
    @app.get("/download/<job_id>/zip")
    def archive(job_id):
        if not session.get("csrf"):
            abort(403, "Требуется локальная сессия приложения")
        job = store.job(job_id)
        if not job["zip_path"]:
            raise ValueError("ZIP ещё не готов")
        return send_file(safe_join(job["root"], job["zip_path"]), mimetype="application/zip", as_attachment=True, download_name="disk-" + job_id[:12] + ".zip")

    return app
