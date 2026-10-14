import io
import os
import secrets
import threading
import json
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, session
from PIL import Image
from werkzeug.exceptions import HTTPException

from .api import APIError, Credentials, DiskAPI, SIZES, Suspended
from .engine import Engine, safe_error
from .paths import remote_path, safe_join
from .store import Store
from .file_types import preview_type
from .updates import Updates
from .diagnostics import bundle, write_bundle
from . import __version__


def create_app(state_dir=None, api=None, port=8766):
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(MAX_CONTENT_LENGTH=1024 * 1024, SESSION_COOKIE_NAME=f"disk_downloader_{port}", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SEND_FILE_MAX_AGE_DEFAULT=0)
    state = Path(state_dir or Path(__file__).resolve().parents[1] / ".state")
    store = Store(state / "jobs.sqlite3")
    credentials = api.credentials if api else Credentials()
    api = api or DiskAPI(credentials)
    engine = Engine(store, api)
    updates = Updates(store)
    app.extensions.update(engine=engine, store=store, disk_api=api, updates=updates)
    thumbnail_slots = threading.BoundedSemaphore(3)
    dialog_lock = threading.Lock()
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
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf):
                abort(403, "Откройте интерфейс приложения: требуется CSRF-токен")
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
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, (ValueError, APIError, Suspended, OSError)):
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else safe_error(exc)), 400
        return jsonify(error="Внутренняя ошибка приложения"), 500

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
        return render_template("index.html", csrf=session["csrf"], version=__version__, default_destination=str(Path(__file__).resolve().parents[1] / "downloads"))

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

    @app.get("/api/profiles")
    def profiles():
        return jsonify(items=[{"name": row["name"], "settings": json.loads(row["settings"])} for row in store.rows("SELECT * FROM profiles ORDER BY name")])

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
        if os.name == "nt":
            os.startfile(safe_join(job["root"], ""), "explore")
        return jsonify(local=local)

    @app.get("/api/status")
    def status():
        jobs = []
        for row in store.rows("SELECT id FROM jobs ORDER BY started DESC LIMIT 30"):
            job = store.job(row["id"])
            jobs.append({k: job[k] for k in ("id", "phase", "started", "root")})
        return jsonify(connected=credentials.connected, jobs=jobs)

    @app.post("/api/connect")
    def connect():
        if any(t.is_alive() for t in engine.threads.values()):
            raise ValueError("Сначала остановите текущее задание и дождитесь остановки")
        credentials.set(payload().get("token"))
        try:
            api.metadata("disk:/", limit=1)
        except BaseException:
            credentials.clear()
            raise
        return jsonify(connected=True)

    @app.post("/api/disconnect")
    def disconnect():
        engine.disconnect()
        return jsonify(connected=False)

    @app.get("/api/browse")
    def browse():
        path = remote_path(request.args.get("path", "disk:/"))
        offset = request.args.get("offset", "0")
        sort = request.args.get("sort", "name")
        if not offset.isdigit() or sort not in ("name", "-name", "size", "-size", "modified", "-modified"):
            raise ValueError("Некорректные параметры списка")
        data = api.metadata(path, {"preview_size": "S"}, offset=int(offset), limit=100, sort=sort)
        embedded = data.get("_embedded", {})
        items = []
        for item in embedded.get("items", []):
            items.append({k: item.get(k) for k in ("path", "name", "type", "size", "modified", "mime_type")} | {"has_preview": preview_type(item) and bool(item.get("preview"))})
        return jsonify(path=path, items=items, total=embedded.get("total"), offset=int(offset), next_offset=int(offset) + len(items), has_more=bool(items) and (embedded.get("total") is None or int(offset) + len(items) < embedded["total"]))

    @app.get("/api/thumbnail")
    def thumbnail():
        if not thumbnail_slots.acquire(blocking=False):
            return "", 429
        response = connection = None
        try:
            path = remote_path(request.args.get("path", ""))
            size = request.args.get("size", "M")
            if size not in SIZES:
                raise ValueError("Некорректный размер миниатюры")
            response, connection = api.media(path, "preview", {"preview_size": size, "retries": 1, "timeout": 15})
            data = bytearray()
            for chunk in response.iter_content(65536):
                if not credentials.connected:
                    raise Suspended("Диск отключён")
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise APIError("Превью слишком большое для миниатюры")
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 16_000_000:
                    raise APIError("Превью слишком большое для миниатюры")
                image.thumbnail((400, 400))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=85)
            output.seek(0)
            return send_file(output, mimetype="image/jpeg")
        finally:
            if response is not None:
                api.close(response, connection)
            thumbnail_slots.release()

    @app.post("/api/folder")
    def folder():
        if not dialog_lock.acquire(blocking=False):
            raise ValueError("Диалог выбора папки уже открыт")
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                directory = filedialog.askdirectory(title="Папка для результатов Яндекс.Диска", parent=root)
            finally:
                root.destroy()
            return jsonify(path=directory)
        finally:
            dialog_lock.release()

    @app.post("/api/jobs")
    def create_job():
        if any(t.is_alive() for t in engine.threads.values()):
            raise ValueError("Сначала завершите или остановите текущую операцию")
        return jsonify(id=engine.create(payload()))

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        return jsonify(engine.status(job_id) | {"connected": credentials.connected})

    @app.get("/api/jobs/<job_id>/errors")
    def job_errors(job_id):
        store.job(job_id)
        offset = request.args.get("offset", "0")
        if not offset.isdigit():
            raise ValueError("Некорректное смещение")
        rows = list(store.rows("SELECT path,status,error,original_status,preview_status,fallback FROM entries WHERE job=? AND kind='file' AND (error!='' OR status IN ('error','partial','no_preview')) UNION ALL SELECT path,'scan_error',error,'not_requested','not_requested',0 FROM scan_errors WHERE job=? LIMIT 100 OFFSET ?", (job_id, job_id, int(offset))))
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
        if os.name == "nt":
            os.startfile(root, "explore")
        return jsonify(path=job["root"], zip_path=job["zip_path"])

    @app.post("/api/jobs/<job_id>/open-journal")
    def open_journal(job_id):
        job = store.job(job_id)
        relative = job["settings"].get("journal_local")
        if not relative:
            raise ValueError("Журнал ещё не сохранён")
        filename = safe_join(job["root"], relative)
        marker = '<!doctype html><html lang="ru" data-journal="' + job_id + ":" + job["settings"].get("journal_key", "") + '">'
        with open(filename, "r", encoding="utf-8") as stream:
            if stream.readline(256).rstrip("\n") != marker:
                raise ValueError("Файл журнала заменён другим файлом")
        if os.name == "nt":
            os.startfile(filename, "open")
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
