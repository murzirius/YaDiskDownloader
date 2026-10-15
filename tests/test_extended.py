import json
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import wait_job, run_job
from disk_downloader.api import APIError, Deadline, Suspended
from disk_downloader.engine import Engine
from disk_downloader.inventory import compare, change_summary, folders
from disk_downloader.issues import draft
from disk_downloader.paths import safe_join
from disk_downloader.rate_limit import RateLimiter
from disk_downloader.runtime import state_folder, destination_folder
from disk_downloader.store import Store
from disk_downloader.tray import Tray
from disk_downloader.web import create_app


def scan(system, **data):
    engine, api, tmp = system
    job_id = engine.create({"destination": str(tmp / "downloads"), "whole_disk": True, "mode": "fallback", "create_zip": False, **data})
    job = wait_job(engine, job_id)
    assert job["phase"] == "ready", job
    return job


def test_exclusions_skip_entire_tree_and_filter_patterns(system):
    engine, api, _ = system
    api.file("disk:/Фото/Черновики/no.jpg")
    api.file("disk:/Фото/good.jpg")
    api.file("disk:/Фото/TEMP.TMP")
    api.file("disk:/data/one.bak")
    api.file("disk:/data/keep.bin")
    job = scan(system, exclusions=["disk:/Фото/Черновики", "*.tmp", "data/*.bak"])
    assert job["files"] == 2
    assert not any(path == "disk:/Фото/Черновики" for path, _, _ in api.metadata_calls)
    engine.action(job["id"], "start")
    done = wait_job(engine, job["id"])
    assert done["originals"] == 2
    assert {path for path, _ in api.media_calls} == {"disk:/Фото/good.jpg", "disk:/data/keep.bin"}


@pytest.mark.parametrize("field,value", [("exclusions", "*.tmp"), ("exclusions", ["../bad"]), ("exclusions", ["C:/bad"]), ("exclusions", ["bad\nname"]), ("speed_limit_mib", True), ("speed_limit_mib", -1), ("speed_limit_mib", float("nan")), ("speed_limit_mib", float("inf"))])
def test_invalid_extended_settings(system, field, value):
    with pytest.raises(ValueError):
        scan(system, **{field: value})


def test_changes_and_folder_totals_across_imports(system):
    engine, api, tmp = system
    api.file("disk:/Фото/keep.jpg", b"123")
    api.file("disk:/Фото/change.jpg", b"a")
    api.file("disk:/gone.jpg", b"deleted")
    api.file("disk:/excluded.jpg", b"later excluded")
    old = scan(system)
    engine.action(old["id"], "start")
    wait_job(engine, old["id"])
    api.file("disk:/Фото/change.jpg", b"changed")
    api.file("disk:/new.jpg", b"new")
    del api.tree["disk:/gone.jpg"]
    job = scan(system, operation="missing", destination=old["root"], exclusions=["disk:/excluded.jpg"])
    changes = {r["path"]: r["status"] for r in engine.store.rows("SELECT path,status FROM changes WHERE job=?", (job["id"],))}
    assert changes == {"disk:/Фото/keep.jpg": "unchanged", "disk:/Фото/change.jpg": "changed", "disk:/gone.jpg": "removed", "disk:/new.jpg": "new", "disk:/excluded.jpg": "excluded"}
    sizes = folders(engine.store, job["id"])
    photo = next(row for row in sizes["items"] if row["path"] == "disk:/Фото")
    assert photo["files"] == 2 and photo["known_bytes"] == 10
    engine.action(job["id"], "start")
    completed = wait_job(engine, job["id"])
    html = Path(safe_join(completed["root"], completed["settings"]["changes_local"])).read_text(encoding="utf-8")
    assert "Не удалось проверить" in html and completed["settings"]["changes_local"] == "changes.html"
    assert Path(old["root"], "gone.jpg").is_file()
    engine.action(job["id"], "pack")
    packed = wait_job(engine, job["id"])
    with zipfile.ZipFile(safe_join(packed["root"], packed["zip_path"])) as z:
        assert any(name.endswith("/changes.html") for name in z.namelist())
        assert "excluded.jpg" not in z.namelist()


def test_portable_inventory_and_failed_scan_are_not_deletions(system):
    engine, api, tmp = system
    api.file("disk:/Blocked/file.jpg")
    old = scan(system)
    engine.action(old["id"], "start")
    wait_job(engine, old["id"])
    other = Engine(Store(tmp / "other-state" / "jobs.sqlite3"), api)
    original_metadata = api.metadata
    def metadata(path, *args, **kwargs):
        if path == "disk:/Blocked":
            raise APIError("Запрещено", 403)
        return original_metadata(path, *args, **kwargs)
    api.metadata = metadata
    job_id = other.create({"destination": old["root"], "operation": "missing", "whole_disk": True})
    job = wait_job(other, job_id)
    assert job["scan_errors"] == 1
    assert change_summary(other.store, job_id)["counts"]["unknown"] == 1
    assert change_summary(other.store, job_id)["counts"]["removed"] == 0


def test_unknown_folder_size_and_literal_percent_path(system):
    engine, api, _ = system
    api.file("disk:/100%_Фото/one.jpg")
    api.file("disk:/100XXФото/two.jpg")
    api.tree["disk:/100%_Фото/one.jpg"].pop("size")
    job = scan(system)
    rows = folders(engine.store, job["id"], "disk:/100%_Фото")["items"]
    assert len(rows) == 1 and rows[0]["files"] == 1 and rows[0]["unknown_sizes"] == 1


@pytest.mark.parametrize("portable", [False, True])
def test_failed_import_keeps_baseline_for_next_comparison(system, portable):
    engine, api, tmp = system
    api.file("disk:/Blocked/file.jpg")
    old = scan(system)
    original = api.metadata
    def blocked(path, *args, **kwargs):
        if path == "disk:/Blocked":
            raise APIError("Запрещено", 403)
        return original(path, *args, **kwargs)
    api.metadata = blocked
    failed = scan(system, operation="missing", destination=old["root"])
    assert change_summary(engine.store, failed["id"])["counts"]["unknown"] == 1
    if portable:
        engine = Engine(Store(tmp / "third-state/jobs.sqlite3"), api)
    api.metadata = original
    job_id = engine.create({"destination": old["root"], "operation": "missing", "whole_disk": True})
    wait_job(engine, job_id)
    assert change_summary(engine.store, job_id)["counts"]["unchanged"] == 1
    assert change_summary(engine.store, job_id)["counts"]["new"] == 0


def test_empty_previous_import_is_still_a_comparison(system):
    engine, api, _ = system
    old = scan(system)
    api.file("disk:/new.jpg")
    job = scan(system, operation="missing", destination=old["root"])
    assert change_summary(engine.store, job["id"])["compared"]
    assert change_summary(engine.store, job["id"])["counts"]["new"] == 1


def test_shared_rate_limit_covers_all_workers():
    limiter = RateLimiter(0.1)
    start = time.monotonic()
    threads = [threading.Thread(target=limiter.consume, args=(25600, lambda: None)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert all(not t.is_alive() for t in threads)
    assert time.monotonic() - start >= 0.9


def test_throttling_hold_preserves_network_deadline_and_cancellation():
    with Deadline(0.1) as deadline:
        with deadline.hold():
            time.sleep(0.16)
            assert not deadline.expired.is_set()
        assert deadline.remaining() > 0.05
        time.sleep(0.14)
        with pytest.raises(APIError):
            deadline.check()
    with Deadline(1, cancelled=lambda: True) as deadline:
        with deadline.hold():
            time.sleep(0.06)
            assert deadline.expired.is_set()


def test_rate_limit_can_be_stopped_immediately():
    limiter = RateLimiter(0.0001)
    def stopped():
        raise Suspended("Остановлено")
    with pytest.raises(Suspended):
        limiter.consume(65536, stopped)


def test_issue_draft_never_includes_local_paths_or_token_automatically(system):
    engine, api, _ = system
    api.file("disk:/PRIVATE_NAME.jpg")
    job = scan(system)
    data = draft({"title": "Ошибка загрузки", "description": "Загрузка не завершается: " + api.credentials.token, "job_id": job["id"]}, engine, "murzirius/YaDiskDownloader")
    assert "PRIVATE_NAME" not in data["body"] and api.credentials.token not in data["body"]
    assert job["root"] not in data["body"]
    url = urlsplit(data["url"])
    assert url.hostname == "github.com" and url.path.endswith("/issues/new")
    assert parse_qs(url.query)["body"] == [data["body"]]


def test_frozen_data_folder_is_outside_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert state_folder() == tmp_path / "local" / "YaDiskDownloader"
    assert "_MEI" not in str(destination_folder())


def test_tray_controls_real_engine_pause_and_resume(system):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    job = scan(system)
    api.block = threading.Event()
    api.delay = 1
    engine.action(job["id"], "start")
    assert api.block.wait(3)
    tray = Tray(engine, "http://127.0.0.1:8766", lambda: None)
    tray.toggle()
    assert engine.controls[job["id"]].paused
    tray.toggle()
    assert not engine.controls[job["id"]].paused
    api.delay = 0
    assert wait_job(engine, job["id"])["phase"] == "completed"


def test_extended_routes_and_csrf(system):
    engine, api, tmp = system
    api.file("disk:/one.jpg")
    app = create_app(tmp / "web-state", api)
    client = app.test_client()
    base = "http://127.0.0.1:8766"
    client.get("/", base_url=base)
    with client.session_transaction(base_url=base) as session:
        csrf = session["csrf"]
    headers = {"Origin": base, "X-CSRF-Token": csrf}
    result = client.post("/api/jobs", base_url=base, headers=headers, json={"destination": str(tmp / "web-results"), "whole_disk": True})
    job_id = result.json["id"]
    wait_job(app.extensions["engine"], job_id)
    for route in ("folders", "changes"):
        assert client.get(f"/api/jobs/{job_id}/{route}", base_url=base, headers=headers).status_code == 200
        assert client.get(f"/api/jobs/{job_id}/{route}", base_url=base).status_code == 403
    issue = client.post("/api/issue-draft", base_url=base, headers=headers, json={"title": "Пример ошибки", "description": "Загрузка завершается с ошибкой", "job_id": job_id})
    assert issue.status_code == 200 and "/issues/new?" in issue.json["url"]
    assert client.post("/api/issue-draft", base_url=base, json={}).status_code == 403
    from disk_downloader import __version__
    assert client.get("/api/runtime", base_url=base, headers=headers).json["version"] == __version__
