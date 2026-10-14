import json
import threading
import time
import zipfile
from pathlib import Path

from conftest import make_job, run_job, wait_job
from disk_downloader.api import APIError
from disk_downloader.engine import Engine
from disk_downloader.journal import Journal
from disk_downloader.store import Store


def read_journal(engine, job_id):
    job = engine.store.job(job_id)
    filename = Path(job["root"], job["settings"]["journal_local"])
    html = filename.read_text(encoding="utf-8")
    payload = html.split('<script id="journal-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    return filename, html, json.loads(payload)


def eventually(check, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("Состояние не изменилось вовремя")


def test_journal_independent_of_report_formats_and_outside_zip(system):
    engine, api, _ = system
    api.file("disk:/Фото/photo.jpg")
    job = run_job(system, mode="both", reports=["json"])
    filename, html, data = read_journal(engine, job["id"])
    assert filename.parent == Path(job["root"])
    assert data["summary"]["phase"] == "Завершено"
    assert data["summary"]["processed"] == 1
    assert {event["category"] for event in data["events"]} >= {"system", "scan", "original", "preview", "archive"}
    assert "Сохранено" in html and "Найти файл или событие" in html
    assert list(Path(job["root"]).glob("log-*.html")) == [filename]
    assert Path(job["root"], job["settings"]["changes_local"]).is_file()
    assert not list(Path(job["root"]).glob("*.part"))
    with zipfile.ZipFile(Path(job["root"], job["zip_path"])) as archive:
        assert not any(name.rsplit("/", 1)[-1].startswith("log-") for name in archive.namelist())
        assert any(name.endswith("/changes.html") for name in archive.namelist())
    assert not engine.controls[job["id"]].journal.thread.is_alive()


def test_periodic_save_including_pause_and_final_stop(system):
    engine, api, _ = system
    assert engine.journal_interval == 300
    engine.journal_interval = 0.08
    api.file("disk:/photo.jpg")
    job_id = make_job(system)
    api.block, api.delay = threading.Event(), 1
    engine.action(job_id, "start")
    assert api.block.wait(3)
    engine.action(job_id, "pause")
    try:
        eventually(lambda: read_journal(engine, job_id)[2]["summary"]["phase"] == "Пауза")
        initial = read_journal(engine, job_id)[2]["summary"]["updated"]
        eventually(lambda: read_journal(engine, job_id)[2]["summary"]["updated"] != initial)
        engine.action(job_id, "stop")
        job = wait_job(engine, job_id)
        assert job["phase"] == "stopped"
        filename, _, data = read_journal(engine, job_id)
        assert data["summary"]["phase"] == "Остановлено"
        assert any("поставлено на паузу" in item["message"] for item in data["events"])
        initial = filename.stat().st_mtime_ns
        time.sleep(0.2)
        assert filename.stat().st_mtime_ns == initial
    finally:
        api.delay = 0
        engine.controls[job_id].stop()
        wait_job(engine, job_id)


def test_history_survives_restart_and_cached_original_is_logged(system):
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    job = run_job(system, mode="fallback")
    filename, _, first = read_journal(engine, job["id"])
    restored = Engine(Store(engine.store.filename), api)
    restored.action(job["id"], "resume")
    wait_job(restored, job["id"])
    later_filename, _, later = read_journal(restored, job["id"])
    assert filename == later_filename
    assert later["events"][:len(first["events"])] == first["events"]
    assert any(event["category"] == "cache" for event in later["events"])
    assert not any(kind == "preview" for _, kind in api.media_calls)


def test_retry_fallback_and_secrets_are_logged_safely(system):
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    secret = api.credentials.token
    message = "Ошибка " + secret + ' https://example.org/transient-link?access_token=other-key </script><img src=x onerror=alert(1)>'
    api.fail[("disk:/photo.jpg", "original")] = [APIError(message, 503), APIError(message, 503)]
    job = run_job(system, mode="fallback")
    _, html, data = read_journal(engine, job["id"])
    assert secret not in html and "transient-link" not in html and "other-key" not in html
    assert "</script><img" not in html
    assert "\\u003c/script\\u003e" in html
    messages = [event["message"] for event in data["events"]]
    assert any("попытка 1/2" in message for message in messages)
    assert any("попытка 2/2" in message for message in messages)
    assert any("вместо недоступного оригинала" in message for message in messages)
    persisted = json.dumps(list(engine.store.rows("SELECT * FROM journal_events WHERE job=?", (job["id"],))))
    assert secret not in persisted and "transient-link" not in persisted


def test_replaced_foreign_journal_is_preserved(system):
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    job_id = make_job(system)
    filename, _, _ = read_journal(engine, job_id)
    filename.write_text("Чужой файл", encoding="utf-8")
    engine.action(job_id, "start")
    wait_job(engine, job_id)
    new_filename, _, data = read_journal(engine, job_id)
    assert filename != new_filename
    assert filename.read_text(encoding="utf-8") == "Чужой файл"
    assert data["summary"]["phase"] == "Завершено"


def test_all_events_preserved_without_truncation(system):
    engine, _, _ = system
    job_id = make_job(system)
    journal = engine.controls[job_id].journal
    for index in range(125):
        journal.event("cache", f"Проверен файл {index}", f"disk:/{index}.jpg", "success")
    journal.flush()
    _, _, data = read_journal(engine, job_id)
    assert sum(event["category"] == "cache" for event in data["events"]) == 125


def test_write_failure_does_not_fail_download_and_retries_final_save(system, monkeypatch):
    import disk_downloader.journal as module
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    job_id = make_job(system)
    real_replace = module.os.replace
    failures = []

    def failing_once(source, destination):
        if str(destination).endswith(".html") and not failures:
            failures.append(True)
            raise PermissionError("Занят файл журнала")
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", failing_once)
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["phase"] == "completed" and failures
    assert not job["journal_error"]
    assert read_journal(engine, job_id)[2]["summary"]["phase"] == "Завершено"
    assert not list(Path(job["root"]).glob("*.part"))


def test_open_journal_route_checks_session_and_preserves_replaced_file(tmp_path, monkeypatch):
    import os
    from test_api_security import web_session
    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions["engine"]
    job_id = engine.create({"destination": str(tmp_path / "results"), "whole_disk": True})
    wait_job(engine, job_id)
    calls = []
    monkeypatch.setattr(os, "startfile", lambda path, verb: calls.append((path, verb)), raising=False)
    url = f"/api/jobs/{job_id}/open-journal"
    assert client.post(url, base_url=base, json={}).status_code == 403
    response = client.post(url, base_url=base, headers=headers, json={})
    assert response.status_code == 200 and "Content-Disposition" not in response.headers
    filename, _, _ = read_journal(engine, job_id)
    assert Path(response.get_json()["path"]) == filename
    if os.name == "nt":
        assert len(calls) == 1 and calls[0][1] == "open"
    filename.write_text("Чужой HTML", encoding="utf-8")
    assert client.post(url, base_url=base, headers=headers, json={}).status_code == 400
    assert len(calls) <= 1


def test_temporary_windows_manifest_lock_does_not_abort_resume(system, monkeypatch):
    import os
    import pytest
    if os.name != "nt":
        pytest.skip("Windows sharing violation")
    import disk_downloader.paths as paths
    engine, api, _ = system
    api.file("disk:/photo.jpg")
    job = run_job(system, mode="fallback")
    replace = paths.os.replace
    locks = []

    def locked_once(source, target):
        if str(target).endswith(".sqlite3") and not locks:
            locks.append(True)
            raise PermissionError(13, "Windows temporarily locked the import manifest")
        return replace(source, target)

    monkeypatch.setattr(paths.os, "replace", locked_once)
    engine.action(job["id"], "resume")
    resumed = wait_job(engine, job["id"])
    assert resumed["phase"] == "completed" and locks
    assert resumed["originals"] == 1
    assert read_journal(engine, job["id"])[2]["summary"]["phase"] == "Завершено"
