import json
import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from conftest import FakeAPI, Response, make_job, run_job, wait_job, test_timeout
from disk_downloader.api import APIError, Credentials, DiskAPI, Suspended
from disk_downloader.web import create_app


def session(app):
    client = app.test_client()
    base = "http://127.0.0.1:8766"
    page = client.get("/", base_url=base)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return client, base, {"X-CSRF-Token": csrf, "Origin": base}


@pytest.mark.parametrize("policy,expected", [("any_result", []), ("originals", [("disk:/photo.jpg", "original")])])
def test_missing_preview_policy_and_progress_before_download(system, policy, expected):
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    first = run_job(system)
    api.media_calls.clear()
    job_id = engine.create({"destination": first["root"], "operation": "missing", "missing_policy": policy, "create_zip": False})
    ready = wait_job(engine, job_id)
    assert ready["phase"] == "ready"
    assert ready["processed"] == (1 if policy == "any_result" else 0)
    assert ready["completeness"] == {"selected": 1, "originals": 0, "preview_only": 1, "no_result": 0}
    assert api.media_calls == []
    engine.action(job_id, "start")
    assert wait_job(engine, job_id)["phase"] == "completed"
    assert api.media_calls == expected


def test_resume_interrupted_preview_policy_and_missing_first(system):
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    job = run_job(system)
    api.file("disk:/new.jpg")
    api.media_calls.clear()
    engine.store.update_job(job["id"], phase="interrupted")
    engine.action(job["id"], "resume", {"missing_policy": "any_result", "create_zip": False})
    done = wait_job(engine, job["id"])
    assert done["phase"] == "completed" and done["previews"] == 1
    assert api.media_calls == []
    missing_id = engine.create({"destination": job["root"], "operation": "missing", "missing_policy": "originals", "create_zip": False})
    wait_job(engine, missing_id)
    engine.action(missing_id, "start")
    assert wait_job(engine, missing_id)["phase"] == "completed"
    assert api.media_calls[0] == ("disk:/new.jpg", "original")


def test_dead_worker_is_resumable(system):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    job_id = make_job(system)
    engine.store.update_job(job_id, phase="downloading")
    status = engine.status(job_id)
    assert status["phase"] == "interrupted" and status["can_resume"] and not status["can_pause"]
    engine.action(job_id, "resume", {"missing_policy": "any_result", "create_zip": False})
    assert wait_job(engine, job_id)["phase"] == "completed"


def test_activity_connection_failure_does_not_leave_running_task(system, monkeypatch):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    job_id = make_job(system)
    @contextmanager
    def broken():
        raise OSError(5, "unavailable")
        yield
    monkeypatch.setattr(engine.store, "activity", broken)
    engine.action(job_id, "start")
    result = wait_job(engine, job_id)
    assert result["phase"] == "failed" and result["can_resume"]


def test_late_401_cannot_revoke_new_key(monkeypatch):
    credentials = Credentials()
    credentials.set("old-test-token-000")
    api = DiskAPI(credentials)
    def opened(*args):
        credentials.set("new-test-token-111")
        return Response(b"", 401), None
    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda r, s: r.close())
    with pytest.raises(Suspended):
        api.get("/resources")
    assert credentials.snapshot()[0] == "new-test-token-111"


def test_reconnect_cannot_undo_disconnect_during_validation():
    credentials = Credentials()
    credentials.set("first-test-key")
    _, epoch = credentials.snapshot()
    credentials.clear()
    with pytest.raises(ValueError, match="другой вкладке"):
        credentials.set("candidate-key", expected_epoch=epoch)
    assert not credentials.connected


def test_preview_policy_accepts_previous_size_and_upgrades_on_request(system):
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    job = run_job(system)
    with engine.store.db() as db:
        db.execute("UPDATE artifacts SET preview_size='S' WHERE job=?", (job["id"],))
    api.media_calls.clear()
    job_id = engine.create({"destination": job["root"], "operation": "missing", "missing_policy": "any_result", "create_zip": False})
    assert wait_job(engine, job_id)["processed"] == 1
    engine.action(job_id, "start")
    assert wait_job(engine, job_id)["phase"] == "completed" and api.media_calls == []
    engine.action(job_id, "retry", {"missing_policy": "originals"})
    result = wait_job(engine, job_id)
    assert result["originals"] == 1 and api.media_calls == [("disk:/photo.jpg", "original")]


def test_stopped_unfinished_files_do_not_show_100_percent(system):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    job_id = make_job(system)
    api.block = threading.Event()
    api.delay = 1
    engine.action(job_id, "start")
    assert api.block.wait(test_timeout(5))
    engine.action(job_id, "stop")
    result = wait_job(engine, job_id)
    assert result["cancelled"] == 1 and result["processed"] == 0 and result["percent"] == 0


def test_failed_key_check_preserves_connection_and_engine_lock(tmp_path):
    fake = FakeAPI()
    entered, release = threading.Event(), threading.Event()
    def verify(token):
        entered.set()
        assert release.wait(test_timeout(5))
        raise APIError("Яндекс.Диск временно недоступен", 503)
    fake.verify_token = verify
    app = create_app(tmp_path / "auth", fake)
    client, base, headers = session(app)
    old = fake.credentials.snapshot()
    result = []
    worker = threading.Thread(target=lambda: result.append(client.post("/api/connect", base_url=base, headers=headers, json={"token": "candidate-test-token"})))
    worker.start()
    try:
        assert entered.wait(test_timeout(2))
        assert app.extensions["engine"].lock.acquire(timeout=.5)
        app.extensions["engine"].lock.release()
        assert fake.credentials.snapshot() == old
    finally:
        release.set()
        worker.join(test_timeout(5))
    assert result[0].status_code == 400 and result[0].json["connected"]
    assert fake.credentials.snapshot() == old
    assert "Яндекс.Диске" in result[0].json["operation"]


def test_reload_keeps_key_and_rejected_request_explains_recovery(tmp_path):
    fake = FakeAPI()
    app = create_app(tmp_path / "reload", fake)
    client, base, headers = session(app)
    before = client.get("/api/status", base_url=base, headers=headers).json
    client.get("/", base_url=base)
    after = client.get("/api/status", base_url=base, headers=headers).json
    assert before["connected"] and before["auth_revision"] == after["auth_revision"]
    rejected = client.post("/api/disconnect", base_url=base, headers=headers | {"X-CSRF-Token": "expired-tab"}, json={})
    assert rejected.status_code == 403 and rejected.json["code"] == "session_expired"
    assert "F5" in rejected.json["error"] and fake.credentials.connected


def test_internal_error_has_reference_without_exception_secrets(tmp_path, monkeypatch):
    app = create_app(tmp_path / "error", FakeAPI())
    client, base, headers = session(app)
    def broken():
        raise RuntimeError("secret-token-and-private-path")
    monkeypatch.setattr(app.extensions["store"], "db", broken)
    response = client.get("/api/status", base_url=base, headers=headers)
    assert response.status_code == 500 and response.json["request_id"]
    assert "secret-token" not in response.text and response.json["operation"]


def test_stalled_socket_pause_resumes_without_spending_attempt(system, monkeypatch):
    engine, fake, _ = system
    body = b"full-original-data" * 40000
    fake.file("disk:/one.bin", body, preview=False, media_type="document", mime_type="application/octet-stream")
    job_id = make_job(system, mode="fallback")
    started, release = threading.Event(), threading.Event()
    offsets = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/resources":
                data = json.dumps(fake.tree["disk:/one.bin"]).encode()
            elif path == "/resources/download":
                data = json.dumps({"href": f"http://127.0.0.1:{self.server.server_port}/media"}).encode()
            else:
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                offsets.append(offset)
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(body)-offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}")
                self.end_headers()
                try:
                    if len(offsets) == 1:
                        self.wfile.write(body[:300000])
                        self.wfile.flush()
                        started.set()
                        release.wait(test_timeout(10))
                    else:
                        self.wfile.write(body[offset:])
                except OSError:
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr("disk_downloader.api.BASE",base)
    monkeypatch.setattr("disk_downloader.api.trusted_media",lambda url: url.startswith(base+"/"))
    engine.api = DiskAPI(fake.credentials)
    try:
        engine.action(job_id,"start",{"attempts":1,"workers":1,"timeout":300,"create_zip":False})
        assert started.wait(test_timeout(5))
        until = time.monotonic() + test_timeout(5)
        while engine.controls[job_id].transferred < 262144 and time.monotonic() < until:
            time.sleep(.01)
        assert engine.controls[job_id].transferred >= 262144
        start = time.monotonic()
        engine.action(job_id,"pause")
        assert time.monotonic()-start < 2
        assert engine.status(job_id)["phase"] == "paused"
        engine.action(job_id,"resume")
        result = wait_job(engine,job_id)
        assert result["phase"] == "completed" and result["errors"] == 0
        assert len(offsets) == 2 and offsets[1] > 0
        assert Path(result["root"],"one.bin").read_bytes() == body
        entry = next(engine.store.rows("SELECT original_attempts FROM entries WHERE job=? AND kind='file'",(job_id,)))
        assert entry["original_attempts"] == 1
    finally:
        release.set()
        engine.controls[job_id].stop()
        server.shutdown()
        server.server_close()
