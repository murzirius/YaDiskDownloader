import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import FakeAPI, Response, make_job, run_job, wait_job
from disk_downloader.api import APIError, Deadline, DiskAPI
from disk_downloader.engine import Control, Engine
from disk_downloader.store import Store


def missing(engine, root, mode="both", **options):
    job_id = engine.create({"destination": str(root), "operation": "missing", "mode": mode, "reports": ["json"], "attempts": 2, **options})
    assert wait_job(engine, job_id)["phase"] == "ready"
    engine.action(job_id, "start")
    return wait_job(engine, job_id)


def test_missing_reuses_root_and_finds_deleted_new_and_changed(system):
    engine, api, _ = system
    api.file("disk:/nested/a.bin", b"version1")
    api.file("disk:/nested/b.bin", b"keep")
    first = run_job(system, mode="both")
    root = Path(first["root"])
    (root / "nested/b.bin.preview.png").unlink()
    api.file("disk:/new.bin")
    api.file("disk:/nested/a.bin", b"version2")
    api.media_calls.clear()
    job = missing(engine, root)
    assert job["root"] == first["root"]
    assert job["processed"] == 3 and job["phase"] == "completed"
    assert ("disk:/nested/b.bin", "original") not in api.media_calls
    assert ("disk:/nested/b.bin", "preview") not in api.media_calls
    assert (root / "nested/a.bin").read_bytes() == b"version1"
    assert len(list((root / "nested").glob("a~*.bin"))) == 1
    assert not list(root.glob("Disk-*"))


def test_portable_import_skips_intact_previews_in_new_state(system, tmp_path):
    engine, api, _ = system
    api.file("disk:/photo.raw")
    first = run_job(system)
    copied = tmp_path / "copied"
    shutil.copytree(first["root"], copied)
    api.media_calls.clear()
    api.fail[("disk:/photo.raw", "original")] = [APIError("Оригинал недоступен", 503) for _ in range(2)]
    restored = Engine(Store(tmp_path / "new-machine" / "state.sqlite3"), api)
    job = missing(restored, copied, mode="previews")
    assert job["existing"] == 1 and job["previews"] == 1
    assert api.media_calls == [("disk:/photo.raw", "original")] * 2
    manifest = next(copied.glob(".disk-import*.sqlite3"))
    assert b"fake-token-for-tests" not in manifest.read_bytes()
    assert b"transient-link" not in manifest.read_bytes()


def test_untracked_original_is_checked_by_remote_checksum(system):
    engine, api, tmp = system
    api.file("disk:/file.bin", b"verified")
    root = tmp / "existing"
    root.mkdir()
    (root / "file.bin").write_bytes(b"verified")
    job = missing(engine, root, mode="fallback", whole_disk=True)
    assert job["existing"] == 1 and api.media_calls == []


def test_missing_corrupt_preview_downloads_once_preserving_bad_file(system):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    first = run_job(system)
    root = Path(first["root"])
    (root / "one.jpg.preview.png").write_bytes(b"damaged")
    api.media_calls.clear()
    api.fail[("disk:/one.jpg", "original")] = [APIError("Оригинал недоступен", 503) for _ in range(2)]
    job = missing(engine, root, mode="previews")
    assert job["previews"] == 1 and api.media_calls.count(("disk:/one.jpg", "preview")) == 1
    assert (root / "one.jpg.preview.png").read_bytes() == b"damaged"


def test_three_attempts_are_total_and_fallback_independent(system):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    api.fail[("disk:/one.bin", "original")] = [APIError("Ошибка", 503) for _ in range(20)]
    job_id = engine.create({"destination": str(tmp / "results"), "whole_disk": True, "mode": "fallback", "attempts": 3})
    wait_job(engine, job_id)
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert api.media_calls.count(("disk:/one.bin", "original")) == 3
    assert api.media_calls.count(("disk:/one.bin", "preview")) == 1
    row = next(engine.store.rows("SELECT original_attempts,preview_attempts FROM entries WHERE job=? AND kind='file'", (job_id,)))
    assert row == {"original_attempts": 3, "preview_attempts": 1}
    assert job["processed"] == 1


def test_link_retries_are_not_multiplied(tmp_path, monkeypatch):
    fake = FakeAPI()
    fake.file("disk:/one.bin")
    api = DiskAPI(fake.credentials)
    monkeypatch.setattr(api, "metadata", fake.metadata)
    calls = []

    def opened(url, *args):
        calls.append(url)
        return (Response(b"", 503, {"Retry-After": "0"}) if url.endswith("/resources/download") else Response(fake.data[("disk:/one.bin", "preview")])), None

    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    engine = Engine(Store(tmp_path / "state.sqlite3"), api)
    job_id = engine.create({"destination": str(tmp_path / "results"), "whole_disk": True, "mode": "fallback", "attempts": 3})
    wait_job(engine, job_id)
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert sum(url.endswith("/resources/download") for url in calls) == 3
    assert len(calls) == 4 and job["previews"] == 1


@pytest.mark.parametrize("phase", ["body", "headers"])
def test_wall_timeout_aborts_slow_live_http_stream(phase, monkeypatch):
    from disk_downloader import api as api_module

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                if phase == "headers":
                    for value in b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n":
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                for _ in range(1000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(api_module, "BASE", "http://127.0.0.1:" + str(server.server_port))
    api = DiskAPI(FakeAPI().credentials)
    started = time.monotonic()
    try:
        with pytest.raises(APIError, match="лимит попытки"):
            api.get("/resources", settings={"attempts": 1, "timeout": 0.2})
        assert time.monotonic() - started < 0.8
    finally:
        server.shutdown()
        server.server_close()


def test_pause_does_not_consume_attempt_budget():
    control = Control()
    with Deadline(0.15, control.check, control.active_clock) as budget:
        control.pause()
        time.sleep(0.25)
        control.resume()
        budget.check()


def test_new_limits_apply_to_existing_idle_job(system):
    engine, api, _ = system
    api.file("disk:/one.bin")
    api.fail[("disk:/one.bin", "original")] = [APIError("Ошибка", 503) for _ in range(10)]
    job_id = make_job(system, mode="fallback")
    engine.action(job_id, "start", {"attempts": 1, "timeout": 5, "workers": 1})
    job = wait_job(engine, job_id)
    assert api.media_calls.count(("disk:/one.bin", "original")) == 1
    assert job["settings"]["attempts"] == 1 and job["settings"]["timeout"] == 5
