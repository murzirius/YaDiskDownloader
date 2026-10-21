import io
import threading
from pathlib import Path

import pytest
from PIL import Image

from conftest import Response, make_job, run_job, wait_job, test_timeout
from disk_downloader.api import APIError
from disk_downloader.engine import Engine
from disk_downloader.paths import publish
from disk_downloader.store import Store


def test_scanning_has_no_percentage_and_resumes_page_offset(system, monkeypatch):
    engine, api, tmp = system
    for i in range(457):
        api.file(f"disk:/{i:04}.dat", preview=False)
    reached = threading.Event()
    blocked = True
    original = api.metadata

    def metadata(path, settings=None, checkpoint=lambda: None, offset=0, limit=200, **kwargs):
        if offset == 200 and blocked:
            reached.set()
            while blocked:
                checkpoint()
                reached.wait(0.01)
        return original(path, settings, checkpoint, offset, limit, **kwargs)

    monkeypatch.setattr(api, "metadata", metadata)
    job_id = engine.create({"destination": str(tmp / "downloads"), "whole_disk": True})
    assert reached.wait(test_timeout(10))
    scanning = engine.status(job_id)
    assert scanning["files"] == 200 and scanning["percent"] is None
    assert scanning["estimate"]["original_bytes"] == 200 * len(b"original")
    engine.action(job_id, "stop")
    wait_job(engine, job_id)
    blocked = False
    before_restart = len(api.metadata_calls)
    restored = Engine(Store(tmp / "state" / "jobs.sqlite3"), api)
    restored.launch(job_id, "scan")
    ready = wait_job(restored, job_id)
    assert ready["phase"] == "ready" and ready["files"] == 457
    # A later page may have been prefetched before stop, but was not committed
    # ahead of the blocked page. Restart must reread it and retain page zero.
    offsets = [offset for path, offset, limit in api.metadata_calls[before_restart:] if limit == 200]
    assert offsets == [200, 400]
    assert sum(offset == 0 for path,offset,limit in api.metadata_calls if limit == 200) == 1


def test_pagination_without_total_ends_only_on_empty_page(system, monkeypatch):
    engine, api, _ = system
    for i in range(403):
        api.file(f"disk:/{i:04}.bin", preview=False)
    original = api.metadata

    def metadata(*args, **kwargs):
        result = original(*args, **kwargs)
        if "_embedded" in result:
            result["_embedded"].pop("total")
        return result

    monkeypatch.setattr(api, "metadata", metadata)
    job_id = make_job(system, whole=True)
    assert engine.status(job_id)["files"] == 403
    assert [offset for path, offset, limit in api.metadata_calls if limit == 200] == [0, 200, 400, 403]


def test_expired_link_retries_do_not_duplicate_progress(system):
    engine, api, _ = system
    api.file("disk:/one.bin")
    api.fail[("disk:/one.bin", "original")] = [APIError("Истёкшая ссылка", 403)]
    job = run_job(system, mode="fallback")
    assert api.media_calls == [("disk:/one.bin", "original"), ("disk:/one.bin", "original")]
    assert job["processed"] == 1 and job["originals"] == 1 and job["previews"] == 0


def test_mismatched_response_size_is_not_confirmed(system, monkeypatch):
    engine, api, _ = system
    api.file("disk:/one.bin", preview=False)

    def media(*args, **kwargs):
        return Response(b"original", headers={"Content-Length": "500"}), None

    monkeypatch.setattr(api, "media", media)
    job = run_job(system, mode="both")
    assert job["errors"] == 1 and job["saved_bytes"] == 0
    assert "неполный ответ" in next(engine.store.rows("SELECT error FROM entries WHERE job=? AND kind='file'", (job["id"],)))["error"]


def test_truncated_jpeg_not_accepted(system):
    engine, api, _ = system
    api.file("disk:/one.bin")
    image = io.BytesIO()
    Image.new("RGB", (128, 128), "blue").save(image, format="JPEG")
    api.data[("disk:/one.bin", "preview")] = image.getvalue()[:-40]
    job = run_job(system)
    assert job["errors"] == 1 and job["previews"] == 0


def test_empty_disk_and_zero_byte_original(system):
    engine, api, _ = system
    empty = run_job(system, whole=True)
    assert empty["phase"] == "completed" and empty["files"] == 0 and empty["percent"] == 100
    api.file("disk:/empty.bin", data=b"", preview=False)
    zero = run_job(system, mode="fallback")
    assert zero["phase"] == "completed" and zero["originals"] == 1
    assert Path(zero["root"], "empty.bin").read_bytes() == b""


def test_publish_never_overwrites(tmp_path):
    temp, target = tmp_path / "file.part", tmp_path / "file.bin"
    temp.write_bytes(b"new")
    target.write_bytes(b"foreign")
    with pytest.raises(FileExistsError):
        publish(temp, target)
    assert target.read_bytes() == b"foreign" and temp.read_bytes() == b"new"
    final = tmp_path / "other.bin"
    publish(temp, final)
    assert final.read_bytes() == b"new" and not temp.exists()


@pytest.fixture(autouse=True)
def short_scan_pages(monkeypatch):
    monkeypatch.setattr('disk_downloader.scanner.PAGE_SIZE',200)
