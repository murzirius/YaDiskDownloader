import os
from pathlib import Path

from conftest import FakeAPI, Response, run_job, wait_job
from disk_downloader.api import APIError, DiskAPI
from disk_downloader.engine import Engine
from disk_downloader.paths import safe_join
from disk_downloader.store import Store


def test_defaults_eight_one_twenty_and_one_actual_attempt(system):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    api.fail[("disk:/one.bin", "original")] = [APIError("Ошибка", 503) for _ in range(5)]
    job_id = engine.create({"destination": str(tmp / "new"), "whole_disk": True, "mode": "fallback"})
    job = wait_job(engine, job_id)
    assert [job["settings"][key] for key in ("workers", "attempts", "timeout")] == [8, 1, 20]
    engine.action(job_id, "start")
    wait_job(engine, job_id)
    assert api.media_calls.count(("disk:/one.bin", "original")) == 1


def test_repeat_original_skips_preview_even_when_preview_mode_selected(system):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    first = run_job(system, mode="fallback")
    api.media_calls.clear()
    job_id = engine.create({"destination": first["root"], "operation": "missing", "mode": "previews"})
    wait_job(engine, job_id)
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert api.media_calls == []
    assert job["existing"] == job["originals"] == 1 and job["previews"] == 0


def test_none_before_preview_before_original_and_corrupt_cache_is_missing(system):
    engine, api, tmp = system
    for name in ("a-preview", "b-original", "c-corrupt", "z-none"):
        api.file("disk:/" + name)
    api.fail[("disk:/a-preview", "original")] = [APIError("Ошибка", 503)]
    api.fail[("disk:/c-corrupt", "original")] = [APIError("Ошибка", 503)]
    api.fail[("disk:/z-none", "original")] = [APIError("Ошибка", 503)]
    api.fail[("disk:/z-none", "preview")] = [APIError("Ошибка", 503)]
    first_id = engine.create({"destination": str(tmp / "results"), "whole_disk": True, "mode": "fallback", "workers": 1, "attempts": 1})
    wait_job(engine, first_id)
    engine.action(first_id, "start")
    first = wait_job(engine, first_id)
    damaged = Path(first["root"]) / "c-corrupt.preview.png"
    old = damaged.read_bytes()
    damaged.write_bytes(b"x" * len(old))
    api.file("disk:/z-new")
    api.media_calls.clear()
    second = engine.create({"destination": first["root"], "operation": "missing", "mode": "previews", "workers": 1})
    wait_job(engine, second)
    engine.action(second, "start")
    job = wait_job(engine, second)
    assert job["phase"] == "completed"
    originals = [path for path, kind in api.media_calls if kind == "original"]
    assert originals == ["disk:/c-corrupt", "disk:/z-new", "disk:/z-none", "disk:/a-preview"]
    assert not any(kind == "preview" for _, kind in api.media_calls)
    assert damaged.read_bytes() == b"x" * len(old)


def test_preview_uses_fresh_process_metadata_without_second_api_call(system, monkeypatch):
    engine, fake, tmp = system
    fake.file("disk:/one.jpg")
    api = DiskAPI(fake.credentials)
    monkeypatch.setattr(api, "metadata", fake.metadata)
    monkeypatch.setattr(api, "open_media", lambda url, preview, settings, checkpoint: (Response(fake.data[("disk:/one.jpg", "preview")]), None))
    monkeypatch.setattr(api, "close", fake.close)
    engine.api = api
    job = run_job(system)
    assert job["phase"] == "completed"
    assert len([call for call in fake.metadata_calls if call[0] == "disk:/one.jpg"]) == 1


def test_retry_preview_requests_fresh_metadata_after_expired_link(system, monkeypatch):
    engine, fake, tmp = system
    fake.file("disk:/one.jpg")
    api = DiskAPI(fake.credentials)
    monkeypatch.setattr(api, "metadata", fake.metadata)
    calls = []
    def opened(url, preview, settings, checkpoint):
        calls.append(url)
        if len(calls) == 1:
            raise APIError("Ссылка истекла", 403)
        return Response(fake.data[("disk:/one.jpg", "preview")]), None
    monkeypatch.setattr(api, "open_media", opened)
    monkeypatch.setattr(api, "close", fake.close)
    engine.api = api
    job = run_job(system)
    assert job["previews"] == 1 and len(calls) == 2
    assert len([call for call in fake.metadata_calls if call[0] == "disk:/one.jpg"]) == 2


def test_local_space_counts_results_reports_archives_and_partial_file(system, monkeypatch):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    job = run_job(system, mode="both")
    expected = job["saved_bytes"]
    for row in engine.store.rows("SELECT local FROM names WHERE job=? AND identity LIKE '@output:%'", (job["id"],)):
        expected += os.path.getsize(safe_join(job["root"], row["local"]))
    assert job["local_bytes"] == expected and job["local_bytes"] > job["saved_bytes"] + job["zip_bytes"]
    partial = Path(job["root"]) / ".disk-test.part"
    partial.write_bytes(b"x" * 2048)
    control = engine.controls[job["id"]]
    control.temporary.add(str(partial))
    assert engine.status(job["id"])["local_bytes"] == expected + 2048
    control.temporary_sizes[str(partial)] = 2048
    original_size = os.path.getsize
    with monkeypatch.context() as simulated:
        simulated.setattr(os.path, "getsize", lambda name: 0 if str(name).endswith(".part") else original_size(name))
        assert engine.status(job["id"])["local_bytes"] == expected + 2048
    assert job["free_bytes"] > 0
    partial.unlink()
    assert engine.status(job["id"])["local_bytes"] == expected
    control.temporary.clear()
    control.temporary_sizes.clear()
    legacy = Path(job["root"]) / job["service"] / "result-legacy.zip"
    legacy.parent.mkdir(exist_ok=True)
    Path(safe_join(job["root"], job["zip_path"])).rename(legacy)
    engine.store.update_job(job["id"], zip_path=job["service"] + "/result-legacy.zip")
    assert engine.status(job["id"])["local_bytes"] == expected


def test_batched_progress_flushes_failed_and_successful_bytes(system, monkeypatch):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    data = api.data[("disk:/one.bin", "preview")]
    job_id = engine.create({"destination": str(tmp / "new"), "whole_disk": True, "attempts": 1})
    wait_job(engine, job_id)
    class Broken(Response):
        def iter_content(self, chunk_size):
            yield b"partial"
            raise APIError("Ошибка потока")
    monkeypatch.setattr(api, "media", lambda *args: (Broken(data), None))
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["downloaded"] == len(b"partial") and job["saved_bytes"] == 0
    assert engine.controls[job_id].temporary == set()
    assert engine.controls[job_id].temporary_sizes == {}
    assert not list(Path(job["root"]).glob("*.part"))
