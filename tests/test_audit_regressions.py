import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from conftest import make_job, run_job, wait_job, test_timeout
from disk_downloader.api import APIError, Credentials, DiskAPI, Suspended, retry_delay, validate_resource
from disk_downloader.engine import Engine, fingerprint
from disk_downloader.inventory import compare
from disk_downloader.paths import PathError, safe_join
from disk_downloader.reports import write_reports
from disk_downloader.store import Store
from disk_downloader.updates import Updates
from disk_downloader.web import create_app


def session(app):
    client = app.test_client()
    base = "http://127.0.0.1:8766"
    client.get("/", base_url=base)
    with client.session_transaction(base_url=base) as state:
        token = state["csrf"]
    return client, base, {"Origin": base, "X-CSRF-Token": token}


@pytest.mark.parametrize("prefix_size", [4, 12])
def test_imported_partial_cannot_truncate_or_delete_normal_file(system, prefix_size):
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system, mode="fallback")
    job = engine.store.job(job_id)
    filename = Path(job["root"]) / "important.txt"
    content = b"KEEP MY DATA"
    filename.write_bytes(content)
    with engine.store.db() as db:
        db.execute("INSERT INTO partials VALUES(?,?,?,?,?,?,?,?,?)", (job_id, "disk:/one.bin", "original", filename.name, "old", prefix_size, hashlib.sha256(content[:prefix_size]).hexdigest(), "", len(content)))
    row, temp, _, _ = engine.partials.prepare(job, "disk:/one.bin", "original", "new", lambda: None)
    assert filename.read_bytes() == content
    assert Path(temp).name != filename.name and row["size"] == 0


def test_partial_hardlink_cannot_change_file_outside_import(system):
    engine, api, tmp = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system, mode="fallback")
    job = engine.store.job(job_id)
    _, temp, _, _ = engine.partials.prepare(job, "disk:/one.bin", "original", "stamp", lambda: None)
    victim = tmp / "outside.txt"
    victim.write_bytes(b"KEEP THIS COMPLETE FILE")
    os.unlink(temp)
    os.link(victim, temp)
    prefix = victim.read_bytes()[:4]
    with engine.store.db() as db:
        db.execute("UPDATE partials SET size=4,sha256=? WHERE job=?", (hashlib.sha256(prefix).hexdigest(), job_id))
    row, replacement, _, _ = engine.partials.prepare(job, "disk:/one.bin", "original", "stamp", lambda: None)
    assert victim.read_bytes() == b"KEEP THIS COMPLETE FILE"
    assert replacement != temp and row["size"] == 0


def test_untrusted_partial_with_service_name_is_read_only(system):
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system)
    job = engine.store.job(job_id)
    victim = Path(job["root"]) / ".disk-document.part"
    content = b"UNCHANGED USER FILE"
    victim.write_bytes(content)
    prefix = content[:4]
    with engine.store.db() as db:
        db.execute("INSERT INTO partials VALUES(?,?,?,?,?,?,?,?,?)", (job_id, "disk:/one.bin", "original", victim.name, "stamp", len(prefix), hashlib.sha256(prefix).hexdigest(), "", len(content)))
    row, copy, _, _ = engine.partials.prepare(job, "disk:/one.bin", "original", "stamp", lambda: None)
    assert copy != str(victim) and Path(copy).read_bytes() == prefix
    assert victim.read_bytes() == content
    engine.partials.discard(job, "disk:/one.bin", "original")
    assert victim.read_bytes() == content and not Path(copy).exists()


@pytest.mark.parametrize("name", ["NUL", "CON.txt", "COM1", "a.", "a ", "a\x00b", "folder/LPT2.bin"])
def test_unsafe_imported_windows_names_rejected(tmp_path, name):
    with pytest.raises(PathError):
        safe_join(tmp_path, name)


def test_busy_job_rejection_does_not_modify_other_task(system, monkeypatch):
    engine, api, tmp = system
    api.file("disk:/one.bin", preview=False)
    first = make_job(system, mode="fallback")
    second = make_job(system, mode="fallback")
    reached = threading.Event()
    api.block, api.delay = reached, 1
    engine.action(first, "start")
    assert reached.wait(test_timeout(10))
    before = engine.store.job(second)["settings"]
    try:
        with pytest.raises(ValueError):
            engine.action(second, "start", {"workers": 1})
        assert engine.store.job(second)["settings"] == before
        count = len(list(engine.store.rows("SELECT id FROM jobs")))
        folders = set((tmp / "downloads").iterdir())
        with pytest.raises(ValueError):
            engine.create({"destination": str(tmp / "downloads"), "whole_disk": True})
        assert len(list(engine.store.rows("SELECT id FROM jobs"))) == count
        assert set((tmp / "downloads").iterdir()) == folders
    finally:
        engine.action(first, "stop")
        api.delay = 0
        wait_job(engine, first)


def test_rejected_selected_retry_keeps_settings(system):
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system)
    before = engine.store.job(job_id)["settings"]
    with pytest.raises(ValueError):
        engine.action(job_id, "retry_selected", {"workers": 1, "paths": ["disk:/not-in-this-job.bin"]})
    assert engine.store.job(job_id)["settings"] == before


def test_failed_import_rolls_back_job_and_empty_folder(system, monkeypatch):
    engine, _, tmp = system
    monkeypatch.setattr(engine, "launch", lambda *args: (_ for _ in ()).throw(ValueError("Test launch failure")))
    with pytest.raises(ValueError):
        engine.create({"destination": str(tmp / "downloads"), "whole_disk": True})
    assert not list(engine.store.rows("SELECT id FROM jobs"))
    assert not list((tmp / "downloads").iterdir())


@pytest.mark.parametrize("value", ["inf", "NaN", "-inf", "99999999999999999999999999999"])
def test_retry_after_is_finite_and_bounded(value):
    assert 0 <= retry_delay(value, 0) <= 300


@pytest.mark.parametrize("patch", [{"size": "123"}, {"size": -1}, {"size": True}, {"md5": "bad"}, {"_embedded": {"items": [], "total": None, "ignored": 1}, "name": []}, {"_embedded": {"items": [], "total": "12"}}])
def test_invalid_metadata_is_a_controlled_error(patch):
    with pytest.raises(APIError):
        validate_resource({"path": "disk:/one.bin", "type": "file", **patch})


def test_bad_child_metadata_does_not_crash_whole_scan(system, monkeypatch):
    engine, api, _ = system
    original = api.metadata
    def metadata(*args, **kwargs):
        value = original(*args, **kwargs)
        if "_embedded" in value:
            value["_embedded"]["items"] = [None]
        return value
    monkeypatch.setattr(api, "metadata", metadata)
    job_id = engine.create({"destination": str(system[2] / "downloads"), "whole_disk": True})
    job = wait_job(engine, job_id)
    assert job["phase"] == "ready" and job["scan_errors"] == 1


def test_md5_only_reuse_checks_source_hash(system):
    engine, api, _ = system
    path = "disk:/one.bin"
    api.file(path, data=b"ORIGINAL", preview=False)
    api.tree[path].pop("sha256")
    job = run_job(system, mode="fallback")
    artifact = next(engine.store.rows("SELECT * FROM artifacts WHERE job=?", (job["id"],)))
    local = Path(job["root"]) / artifact["local"]
    local.write_bytes(b"MODIFIED")
    with engine.store.db() as db:
        db.execute("UPDATE artifacts SET sha256=? WHERE job=?", (hashlib.sha256(b"MODIFIED").hexdigest(), job["id"]))
        db.execute("DELETE FROM verification_cache WHERE job=?", (job["id"],))
    engine.action(job["id"], "resume")
    done = wait_job(engine, job["id"])
    new = next(engine.store.rows("SELECT * FROM artifacts WHERE job=?", (job["id"],)))
    assert done["errors"] == 0 and (Path(done["root"]) / new["local"]).read_bytes() == b"ORIGINAL"
    assert local.read_bytes() == b"MODIFIED"


def test_preview_has_encoded_size_limit(system, monkeypatch):
    engine, api, _ = system
    api.file("disk:/one.png")
    api.data[("disk:/one.png", "preview")] += b"x" * 1500
    monkeypatch.setattr("disk_downloader.engine.PREVIEW_MAX_BYTES", 512)
    job = run_job(system, mode="previews")
    assert job["errors"] == 1 and job["previews"] == 0


def test_compare_can_checkpoint_without_holding_write_lock(system):
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system)
    def checkpoint():
        engine.store.update_job(job_id, error="checkpoint ran")
    compare(engine.store, job_id, checkpoint)
    assert engine.store.job(job_id)["error"] == "checkpoint ran"


def test_reports_can_be_cancelled_and_clean_temp_files(system):
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system)
    root = Path(engine.store.job(job_id)["root"])
    def checkpoint():
        raise Suspended("Stop")
    with pytest.raises(Suspended):
        write_reports(engine.store, job_id, checkpoint)
    assert not list(root.glob(".disk-*.part"))


def test_corrupt_jobs_and_profiles_do_not_prevent_startup(system):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    job_id = make_job(system)
    with engine.store.db() as db:
        db.execute("UPDATE jobs SET settings='broken JSON' WHERE id=?", (job_id,))
        db.execute("INSERT INTO profiles VALUES('corrupt','[]')")
    restored = Store(engine.store.filename)
    assert restored.job(job_id)["settings"]["_invalid"]
    assert restored.summary(job_id)["phase"] == "failed"
    recovered = Engine(restored, api)
    with pytest.raises(ValueError):
        recovered.launch(job_id)
    app = create_app(tmp / "state", api=api)
    client, base, headers = session(app)
    result = client.get("/api/profiles", base_url=base, headers=headers)
    assert result.status_code == 200 and result.json["items"][0]["error"]


def test_legacy_retries_remain_retries_after_upgrade(system):
    engine, api, _ = system
    api.file("disk:/one.bin")
    job_id = make_job(system)
    value = engine.store.job(job_id)["settings"]
    value.pop("attempts")
    value["retries"] = 2
    engine.store.update_job(job_id, settings=json.dumps(value))
    restored = Store(engine.store.filename)
    assert restored.job(job_id)["settings"]["attempts"] == 3


def test_corrupt_update_settings_and_partial_save_are_safe(system):
    store = system[0].store
    updater = Updates(store)
    with store.db() as db:
        db.execute("INSERT INTO preferences VALUES('updates','[]')")
    assert not updater.settings()["automatic"]
    updater.save({"repository": "example/project", "automatic": False})
    assert updater.save({"automatic": True})["repository"] == "example/project"


def test_credentials_reject_unicode_header_characters():
    with pytest.raises(ValueError):
        Credentials().set("token-with-кириллица")


@pytest.mark.parametrize("attempts", [1, 2])
def test_broken_media_stream_uses_retries_and_recovered_preview_is_not_error(system, monkeypatch, attempts):
    import requests
    from conftest import Response
    from disk_downloader.network import NetworkWait
    engine, api, _ = system
    path = "disk:/one.jpg"
    api.file(path)
    job_id = make_job(system, mode="fallback")
    original = api.media
    calls = []
    class Broken(Response):
        def iter_content(self, chunk_size):
            raise requests.exceptions.ChunkedEncodingError("private network error")
            yield b""
    def media(path, kind, settings, checkpoint):
        calls.append(kind)
        if kind == "original" and calls.count("original") == 1:
            return Broken(b"original"), None
        return original(path, kind, settings, checkpoint)
    monkeypatch.setattr(NetworkWait, "reachable", staticmethod(lambda: True))
    monkeypatch.setattr(api, "media", media)
    engine.action(job_id, "start", {"attempts": attempts})
    done = wait_job(engine, job_id)
    assert done["errors"] == 0
    assert calls.count("original") == attempts
    assert done["previews"] == (attempts == 1)
    assert done["originals"] == (attempts == 2)


def test_thumbnail_wall_timeout_releases_slots(system, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from disk_downloader import api as module
    from disk_downloader.api import Deadline
    api = DiskAPI(system[1].credentials)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10000")
            self.end_headers()
            try:
                for _ in range(1000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/preview"
    monkeypatch.setattr(module, "trusted_media", lambda value: value == url)
    monkeypatch.setattr(api, "media", lambda path, kind, settings, checkpoint: api.open_media(url, False, settings, checkpoint))
    monkeypatch.setattr("disk_downloader.web.Deadline", lambda seconds, *args, **kwargs: Deadline(0.2, *args, **kwargs))
    app = create_app(system[2] / "thumbnail-state", api=api)
    client, base, headers = session(app)
    try:
        for _ in range(4):
            started = time.monotonic()
            result = client.get("/api/thumbnail?path=disk:/one.jpg", base_url=base, headers=headers)
            assert result.status_code == 400 and time.monotonic() - started < 1
    finally:
        server.shutdown()
        server.server_close()


def test_retry_wait_does_not_consume_next_request_budget(system, monkeypatch):
    from conftest import Response
    api = DiskAPI(system[1].credentials)
    responses = [Response(b"", 429, {"Retry-After": "0.15"}), Response(b'{"ok":true}')]
    def opened(*args):
        return responses.pop(0), None
    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    assert api.get("/resources", settings={"attempts": 2, "timeout": 0.05}) == {"ok": True}


def test_external_manifest_cache_is_not_trusted(system):
    from disk_downloader.import_state import find_import, inherit
    engine, api, tmp = system
    api.file("disk:/one.jpg")
    old = run_job(system, mode="fallback")
    fresh_store = Store(tmp / "separate" / "jobs.sqlite3")
    previous = find_import(fresh_store, Path(old["root"]))
    assert previous and previous.get("file")
    with fresh_store.db() as db:
        db.execute("INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)", ("new", json.dumps(old["settings"]), old["root"], old["service"], "new", old["started"]))
    inherit(fresh_store, "new", previous)
    assert list(fresh_store.rows("SELECT * FROM artifacts WHERE job='new'"))
    assert not list(fresh_store.rows("SELECT * FROM verification_cache WHERE job='new'"))


def test_non_ascii_csrf_is_rejected_without_server_error(system):
    app = create_app(system[2] / "csrf-state", api=system[1])
    client, base, headers = session(app)
    headers["X-CSRF-Token"] = "invalid-é"
    assert client.get("/api/status", base_url=base, headers=headers).status_code == 403


def test_diagnostics_ids_link_within_one_bundle_only(system):
    from disk_downloader.diagnostics import bundle
    engine, api, _ = system
    path = "disk:/personal.jpg"
    api.file(path, preview=False)
    api.fail[(path, "original")] = [APIError("HTTP 403", 403)] * 10
    job = run_job(system, mode="fallback")
    first, second = bundle(engine, job["id"]), bundle(engine, job["id"])
    identity = first["errors.json"][0]["source_id"]
    assert identity != second["errors.json"][0]["source_id"]
    assert any(event.get("source_id") == identity for event in first["events.json"])
    assert path not in json.dumps(first)


def test_reused_preview_must_decode_successfully(system):
    import io
    from PIL import Image
    engine, api, _ = system
    path = "disk:/one.jpg"
    api.file(path)
    job = run_job(system, mode="previews")
    row = next(engine.store.rows("SELECT * FROM artifacts WHERE job=?", (job["id"],)))
    local = Path(job["root"]) / row["local"]
    buffer = io.BytesIO()
    Image.new("RGB", (128, 128), "blue").save(buffer, format="JPEG")
    damaged = buffer.getvalue()[:-40]
    local.write_bytes(damaged)
    with engine.store.db() as db:
        db.execute("UPDATE artifacts SET size=?,sha256=? WHERE job=?", (len(damaged), hashlib.sha256(damaged).hexdigest(), job["id"]))
        db.execute("DELETE FROM verification_cache WHERE job=?", (job["id"],))
    entry = next(engine.store.rows("SELECT * FROM entries WHERE job=? AND kind='file'", (job["id"],)))
    from disk_downloader.engine import Control
    assert not engine.reusable(job, entry, "preview", api.tree[path], Control())
    assert local.read_bytes() == damaged


@pytest.mark.parametrize("command", ["pause", "stop"])
def test_report_stage_accepts_controller_commands(system, monkeypatch, command):
    import disk_downloader.engine as module
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)
    job_id = make_job(system, mode="fallback")
    reached, release = threading.Event(), threading.Event()
    original = module.write_reports
    def reports(*args, **kwargs):
        reached.set()
        while not release.is_set():
            args[2]()
            time.sleep(0.01)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "write_reports", reports)
    engine.action(job_id, "start", {"create_zip": False})
    try:
        assert reached.wait(test_timeout(15))
        assert engine.store.job(job_id)["phase"] == "preparing"
        engine.action(job_id, command)
        if command == "pause":
            assert engine.store.job(job_id)["phase"] == "paused"
            engine.action(job_id, "resume")
            release.set()
            assert wait_job(engine, job_id)["phase"] == "completed"
        else:
            assert wait_job(engine, job_id)["phase"] == "stopped"
    finally:
        release.set()
        engine.controls[job_id].stop()


def test_manifest_with_null_generation_is_ignored(system):
    import sqlite3
    from disk_downloader.import_state import FORMAT, find_import
    engine, _, tmp = system
    root = tmp / "foreign"
    root.mkdir()
    filename = root / ".disk-import.sqlite3"
    with sqlite3.connect(filename) as db:
        db.execute("CREATE TABLE import_meta(key TEXT,value TEXT)")
        db.executemany("INSERT INTO import_meta VALUES(?,?)", [("format", FORMAT), ("settings", json.dumps({"paths": ["disk:/"]})), ("service", "safe"), ("generation", None)])
    assert find_import(engine.store, root) is None
    assert filename.exists()
