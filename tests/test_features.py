import hashlib
import json
import os
import shutil
import threading
import zipfile
from pathlib import Path

import pytest

from conftest import FakeAPI, Response, make_job, run_job, wait_job
from disk_downloader.adaptive import AdaptiveWorkers
from disk_downloader.api import APIError
from disk_downloader.diagnostics import bundle, write_bundle
from disk_downloader.engine import Engine
from disk_downloader.store import Store
from disk_downloader.updates import Updates, repository
from test_api_security import web_session


REAL_UPDATE_START = Updates.start


def test_zip_optional_and_manual_pack(system):
    engine, api, tmp = system
    api.file("disk:/photo.jpg")
    job_id = engine.create({"destination": str(tmp / "output"), "whole_disk": True, "mode": "fallback", "create_zip": False})
    ready = wait_job(engine, job_id)
    assert ready["estimate"]["zip_basis_bytes"] == 0
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["phase"] == "completed" and job["zip_path"] is None and job["generation"] == 1
    root = Path(job["root"])
    assert (root / "photo.jpg").is_file() and (root / "report-1.json").is_file()
    assert not list(root.glob("*.zip"))
    engine.action(job_id, "pack")
    packed = wait_job(engine, job_id)
    assert packed["zip_path"] == "result-2.zip" and (root / "report-2.json").is_file()


def test_auto_pause_disk_percentage_and_changed_threshold(system, monkeypatch):
    engine, api, tmp = system
    api.file("disk:/photo.jpg")
    sample = shutil.disk_usage(tmp)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(sample)(10**11, 96 * 10**9, 4 * 10**9))
    job_id = engine.create({"destination": str(tmp / "output"), "whole_disk": True, "max_disk_percent": 95})
    paused = wait_job(engine, job_id, phases={"paused"})
    assert paused["live"] and "96.0%" in paused["pause_reason"]
    try:
        with pytest.raises(ValueError, match="Автопауза"):
            engine.action(job_id, "resume")
        engine.action(job_id, "resume", {"max_disk_percent": 98})
        assert wait_job(engine, job_id)["phase"] == "ready"
    finally:
        engine.controls[job_id].stop()


def test_auto_pause_during_transfer_retains_budget_and_resumes(system, monkeypatch):
    engine, api, tmp = system
    api.file("disk:/photo.jpg", data=b"a" * 100)
    job_id = make_job(system, mode="fallback")
    sample = shutil.disk_usage(tmp)
    full = [False]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(sample)(10**11, 99 * 10**9 if full[0] else 50 * 10**9, 10**9 if full[0] else 50 * 10**9))
    original = api.media

    class FillingResponse(Response):
        def iter_content(self, chunk_size):
            yield self.data[:10]
            full[0] = True
            engine.controls[job_id].next_space_check = 0
            yield self.data[10:]

    def media(*args, **kwargs):
        response, session = original(*args, **kwargs)
        return FillingResponse(response.data), session

    monkeypatch.setattr(api, "media", media)
    engine.action(job_id, "start", {"attempts": 1})
    wait_job(engine, job_id, phases={"paused"})
    try:
        full[0] = False
        engine.action(job_id, "resume")
        assert wait_job(engine, job_id)["phase"] == "completed"
        assert api.media_calls.count(("disk:/photo.jpg", "original")) == 1
    finally:
        full[0] = False
        engine.controls[job_id].stop()


def test_selected_retry_only_requested_files(system):
    engine, api, _ = system
    for name in ("one.jpg", "two.jpg"):
        api.file("disk:/" + name, preview=False)
        api.fail[("disk:/" + name, "original")] = [APIError("Отказ", 403)] * 2
    job = run_job(system, mode="fallback")
    before = list(api.media_calls)
    engine.action(job["id"], "retry_selected", {"paths": ["disk:/one.jpg"], "attempts": 1})
    retried = wait_job(engine, job["id"])
    assert retried["originals"] == 1
    assert api.media_calls[len(before):] == [("disk:/one.jpg", "original")]
    with pytest.raises(ValueError):
        engine.action(job["id"], "retry_selected", {"paths": ["disk:/foreign.jpg"]})


class InterruptedResponse(Response):
    def iter_content(self, chunk_size):
        yield self.data[:5]
        raise APIError("Сетевая ошибка")


def prepare_interrupted(system, monkeypatch):
    engine, api, _ = system
    data = b"original-content-for-range-test"
    api.file("disk:/large.bin", data=data)
    original = api.media
    first = [True]
    offsets = []

    def media(path, kind, settings, checkpoint=lambda: None):
        if kind == "original":
            offsets.append(settings.get("_range_offset", 0))
            if first[0]:
                first[0] = False
                api.media_calls.append((path, kind))
                return InterruptedResponse(data, headers={"Content-Length": str(len(data)), "ETag": '"original-v1"'}), None
        return original(path, kind, settings, checkpoint)

    monkeypatch.setattr(api, "media", media)
    job_id = make_job(system, mode="fallback")
    engine.action(job_id, "start", {"attempts": 1, "create_zip": False})
    wait_job(engine, job_id)
    assert engine.status(job_id)["partial_bytes"] == 5
    return job_id, data, offsets


def test_range_resume_after_restart_and_checksum(system, monkeypatch):
    engine, api, _ = system
    job_id, data, offsets = prepare_interrupted(system, monkeypatch)
    restored = Engine(Store(engine.store.filename), api)

    def resume(path, kind, settings, checkpoint=lambda: None):
        offset = settings.get("_range_offset", 0)
        offsets.append(offset)
        assert settings.get("_range_etag") == '"original-v1"'
        return Response(data[offset:], status=206, headers={"Content-Length": str(len(data) - offset), "Content-Range": f"bytes {offset}-{len(data)-1}/{len(data)}", "ETag": '"original-v1"'}), None

    monkeypatch.setattr(api, "media", resume)
    restored.action(job_id, "retry_selected", {"paths": ["disk:/large.bin"]})
    result = wait_job(restored, job_id)
    assert offsets[-1] == 5 and result["partial_bytes"] == 0
    assert Path(result["root"], "large.bin").read_bytes() == data
    assert result["downloaded"] == len(data) + 0 + len(api.data[("disk:/large.bin", "preview")])


def test_server_ignoring_range_restarts_without_duplicate_bytes(system, monkeypatch):
    engine, api, _ = system
    job_id, data, offsets = prepare_interrupted(system, monkeypatch)

    def ignored(path, kind, settings, checkpoint=lambda: None):
        offsets.append(settings.get("_range_offset", 0))
        return Response(data), None

    monkeypatch.setattr(api, "media", ignored)
    engine.action(job_id, "retry_selected", {"paths": ["disk:/large.bin"]})
    job = wait_job(engine, job_id)
    assert offsets[-1] == 5 and Path(job["root"], "large.bin").read_bytes() == data


@pytest.mark.parametrize("change", ["bad_range", "changed_etag", "damaged_local"])
def test_resume_does_not_mix_incompatible_parts(system, monkeypatch, change):
    engine, api, _ = system
    job_id, data, offsets = prepare_interrupted(system, monkeypatch)
    row = next(engine.store.rows("SELECT * FROM partials WHERE job=?", (job_id,)))
    old = Path(engine.store.job(job_id)["root"], row["local"])
    if change == "damaged_local":
        old.write_bytes(b"foreign-data")

    def incompatible(path, kind, settings, checkpoint=lambda: None):
        offset = settings.get("_range_offset", 0)
        if not offset:
            return Response(data), None
        begin = 0 if change == "bad_range" else offset
        return Response(data[offset:], status=206, headers={"Content-Length": str(len(data) - offset), "Content-Range": f"bytes {begin}-{len(data)-1}/{len(data)}", "ETag": '"changed"' if change == "changed_etag" else '"original-v1"'}), None

    monkeypatch.setattr(api, "media", incompatible)
    engine.action(job_id, "retry_selected", {"paths": ["disk:/large.bin"]})
    job = wait_job(engine, job_id)
    if change == "damaged_local":
        assert Path(job["root"], "large.bin").read_bytes() == data
        assert old.read_bytes() == b"foreign-data"
    else:
        assert job["originals"] == 0 and not Path(job["root"], "large.bin").exists()


def test_profile_roundtrip_has_no_credentials_or_service_paths(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    settings = {"destination": str(tmp_path / "output"), "paths": [], "mode": "fallback", "token": "fake-token-for-tests", "manifest_local": "secret", "journal_key": "secret", "create_zip": False, "max_disk_percent": 97}
    response = client.post("/api/profiles", base_url=base, headers=headers, json={"name": "Фото", "settings": settings})
    assert response.status_code == 200
    profiles = client.get("/api/profiles", base_url=base, headers=headers).get_json()["items"]
    assert profiles[0]["settings"]["paths"] == [] and not profiles[0]["settings"]["whole_disk"]
    assert profiles[0]["settings"]["max_disk_percent"] == 97
    assert "secret" not in json.dumps(profiles) and "fake-token" not in json.dumps(profiles)
    assert client.post("/api/profiles", base_url=base, headers=headers, json={"name": "Фото", "delete": True}).status_code == 200
    assert not client.get("/api/profiles", base_url=base, headers=headers).get_json()["items"]


def test_adaptive_workers_reduce_recover_and_stay_bounded():
    clock = [0]
    adaptive = AdaptiveWorkers(8, clock=lambda: clock[0])
    adaptive.failure(429)
    assert adaptive.limit() == 4
    clock[0] = 3
    adaptive.failure(503)
    assert adaptive.limit() == 2
    clock[0] = 10
    for _ in range(8):
        adaptive.success()
    assert adaptive.limit() == 3
    adaptive.configure(False)
    adaptive.failure(429)
    assert adaptive.limit() == 8


def test_adaptive_speed_requires_sustained_slowdown():
    clock = [0]
    adaptive = AdaptiveWorkers(8, clock=lambda: clock[0])
    clock[0] = 5
    adaptive.measure(100000, 8)
    for index in range(1, 5):
        clock[0] = 5 + index * 5
        adaptive.measure(100000 + 1000 * index, 8)
    assert adaptive.limit() == 7


@pytest.mark.parametrize("value", ["https://evil.test/a/b", "https://github.com/a/b?token=secret", "../repo", "https://user:password@github.com/a/b", "a/b/extra"])
def test_update_repository_rejects_untrusted_address(value):
    with pytest.raises(ValueError):
        repository(value)


def test_updates_use_github_without_disk_token(system, monkeypatch):
    import disk_downloader.updates as module
    updater = Updates(system[0].store)
    calls = []

    class GitHubResponse:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def iter_content(self, size):
            yield json.dumps({"tag_name": "v9.0.0", "html_url": "https://github.com/murzirius/YaDiskDownloader/releases/tag/v9.0.0"}).encode()

    def get(self, url, **kwargs):
        calls.append((url, kwargs))
        return GitHubResponse()

    monkeypatch.setattr(module.requests.Session, "get", get)
    updater.check()
    result = updater.snapshot()
    assert result["available"] and result["latest"] == "v9.0.0"
    assert "api.github.com/repos/murzirius/YaDiskDownloader/releases/latest" in calls[0][0]
    assert "Authorization" not in calls[0][1]["headers"] and "fake-token" not in json.dumps(calls)


def test_diagnostics_contains_no_paths_names_tokens_or_contents(system):
    engine, api, _ = system
    api.file("disk:/Приватное/private-name.jpg", data=b"private-content")
    api.fail[("disk:/Приватное/private-name.jpg", "original")] = [APIError("fake-token-for-tests https://example.test/key private-name.jpg", 503)] * 2
    job = run_job(system, mode="fallback")
    payload = json.dumps(bundle(engine, job["id"]), ensure_ascii=False)
    for secret in ("private-name", "Приватное", "fake-token", "example.test", "private-content", job["root"]):
        assert secret not in payload
    local = write_bundle(engine, job["id"])
    with zipfile.ZipFile(Path(job["root"], local)) as archive:
        assert archive.testzip() is None and "summary.json" in archive.namelist()
        assert not any(name.endswith(".sqlite3") for name in archive.namelist())


def test_real_http_range_headers_and_resumed_body(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from disk_downloader.api import DiskAPI
    import disk_downloader.api as module
    calls = []
    data = b"the-original-http-body"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append(dict(self.headers))
            assert self.headers["Range"] == "bytes=5-"
            assert self.headers["If-Range"] == '"etag-v1"'
            assert "Authorization" not in self.headers
            self.send_response(206)
            self.send_header("Content-Length", str(len(data)-5))
            self.send_header("Content-Range", f"bytes 5-{len(data)-1}/{len(data)}")
            self.end_headers()
            self.wfile.write(data[5:])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/file"
    monkeypatch.setattr(module, "trusted_media", lambda target: target == url)
    api = DiskAPI(FakeAPI().credentials)
    try:
        response, session = api.open_media(url, False, {"_range_offset": 5, "_range_etag": '"etag-v1"', "timeout": 5})
        try:
            assert response.status_code == 206 and b"".join(response.iter_content(1024)) == data[5:]
        finally:
            api.close(response, session)
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_portable_import_keeps_partial_for_missing_download(system, monkeypatch, tmp_path):
    engine, api, _ = system
    job_id, data, offsets = prepare_interrupted(system, monkeypatch)
    original_job = engine.store.job(job_id)
    restored = Engine(Store(tmp_path / "new-state" / "jobs.sqlite3"), api)

    def resumed(path, kind, settings, checkpoint=lambda: None):
        offset = settings.get("_range_offset", 0)
        offsets.append(offset)
        return Response(data[offset:], status=206 if offset else 200, headers={"Content-Length": str(len(data)-offset), "Content-Range": f"bytes {offset}-{len(data)-1}/{len(data)}", "ETag": '"original-v1"'}), None

    monkeypatch.setattr(api, "media", resumed)
    missing_id = restored.create({"destination": original_job["root"], "operation": "missing", "create_zip": False})
    assert wait_job(restored, missing_id)["phase"] == "ready"
    restored.action(missing_id, "start", {"attempts": 1})
    job = wait_job(restored, missing_id)
    assert offsets[-1] == 5 and Path(job["root"], "large.bin").read_bytes() == data


def test_actual_stop_retains_original_part(system, monkeypatch):
    from disk_downloader.api import Suspended
    engine, api, _ = system
    data = b"original-stop-test"
    api.file("disk:/large.bin", data=data)
    job_id = make_job(system, mode="fallback")

    class StoppedResponse(Response):
        def iter_content(self, size):
            yield data[:5]
            engine.controls[job_id].stop()
            raise Suspended("Задание остановлено")

    monkeypatch.setattr(api, "media", lambda *args, **kwargs: (StoppedResponse(data), None))
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["phase"] == "stopped" and job["partial_bytes"] == 5


def test_selected_retry_leaves_other_pending_file_untouched(system):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    api.file("disk:/two.jpg")
    job_id = make_job(system, mode="fallback")
    engine.action(job_id, "retry_selected", {"paths": ["disk:/one.jpg"], "create_zip": False})
    job = wait_job(engine, job_id)
    assert job["phase"] == "stopped" and job["originals"] == 1
    assert ("disk:/two.jpg", "original") not in api.media_calls


def test_diagnostics_routes_preview_then_save_local_archive(tmp_path, monkeypatch):
    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions["engine"]
    job_id = engine.create({"destination": str(tmp_path / "results"), "whole_disk": True})
    job = wait_job(engine, job_id)
    calls = []
    monkeypatch.setattr(os, "startfile", lambda path, verb: calls.append((path, verb)), raising=False)
    url = f"/api/jobs/{job_id}/diagnostics"
    assert client.post(url, base_url=base, json={}).status_code == 403
    preview = client.get(url + "-preview", base_url=base, headers=headers)
    assert preview.status_code == 200 and "environment.json" in preview.get_json()["files"]
    response = client.post(url, base_url=base, headers=headers, json={})
    assert response.status_code == 200 and "Content-Disposition" not in response.headers
    local = response.get_json()["local"]
    assert Path(job["root"], local).is_file()
    if os.name == "nt":
        assert calls and calls[0][1] == "explore"
