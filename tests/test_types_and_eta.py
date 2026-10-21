import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import wait_job
from disk_downloader.engine import Control
from disk_downloader.estimation import RemainingTime
from disk_downloader.file_types import FILE_TYPES, file_type
from disk_downloader.paths import safe_join


def mixed(api):
    api.file("disk:/nested/photo.jpg", media_type="image", mime_type="image/jpeg")
    api.file("disk:/nested/clip.mp4", media_type="video", mime_type="video/mp4")
    api.file("disk:/nested/music.mp3", media_type="audio", mime_type="audio/mpeg")
    api.file("disk:/nested/document.pdf", media_type="document", mime_type="application/pdf")


def download(engine, root, **options):
    job_id = engine.create({"destination": str(root), "whole_disk": True, "reports": ["json"], **options})
    assert wait_job(engine, job_id)["phase"] == "ready"
    engine.action(job_id, "start")
    return wait_job(engine, job_id)


@pytest.mark.parametrize("meta,expected", [
    ({"name": "image.bin", "media_type": "image"}, "photos"),
    ({"name": "photo.JPG", "mime_type": "video/mp4"}, "videos"),
    ({"name": "unknown", "mime_type": "audio/flac"}, "music"),
    ({"name": "unknown", "media_type": "audio", "mime_type": "image/png"}, "music"),
    ({"name": "RAW.NEF", "mime_type": "application/octet-stream"}, "photos"),
    ({"name": "clip.MOV"}, "videos"),
    ({"name": "song.OPUS"}, "music"),
    ({"name": "report.pdf", "media_type": "document"}, "files"),
    ({"name": "no-extension"}, "files"),
])
def test_type_uses_metadata_then_extension(meta, expected):
    assert file_type(meta) == expected


def test_filter_whole_disk_and_mixed_preview_rules(system):
    engine, api, tmp = system
    mixed(api)
    filtered = download(engine, tmp / "selected", file_types=["photos", "videos"])
    assert filtered["files"] == filtered["previews"] == 2 and filtered["originals"] == 0
    assert set(api.media_calls) == {("disk:/nested/photo.jpg", "preview"), ("disk:/nested/clip.mp4", "preview")}
    api.media_calls.clear()
    all_types = download(engine, tmp / "all", file_types=list(FILE_TYPES))
    assert all_types["phase"] == "completed" and all_types["files"] == 4
    assert all_types["originals"] == all_types["previews"] == 2
    assert ("disk:/nested/music.mp3", "original") in api.media_calls
    assert ("disk:/nested/document.pdf", "original") in api.media_calls
    assert ("disk:/nested/music.mp3", "preview") not in api.media_calls
    assert ("disk:/nested/document.pdf", "preview") not in api.media_calls


def test_filter_does_not_stop_pagination(system):
    engine, api, tmp = system
    for index in range(1107):
        music = index % 2 == 1
        api.file(f"disk:/nested/{index:04}.bin", preview=False, media_type="audio" if music else "image", mime_type="audio/mpeg" if music else "image/png")
    job_id = engine.create({"destination": str(tmp / "results"), "whole_disk": True, "file_types": ["music"]})
    job = wait_job(engine, job_id)
    assert job["files"] == 553
    # Parallel requests may reach the server in any order. Every page must
    # still be fetched exactly once, and filtering must retain every match.
    assert sorted(offset for path, offset, limit in api.metadata_calls if path == "disk:/nested" and limit == 200) == [0, 200, 400, 600, 800, 1000]
    assert {row["path"] for row in engine.store.rows("SELECT path FROM entries WHERE job=? AND kind='file'", (job_id,))} == {
        f"disk:/nested/{index:04}.bin" for index in range(1, 1107, 2)
    }
    assert job["estimate"]["known_bytes"] == 553 * len(b"original")


def test_music_failure_never_requests_cover_preview(system):
    from disk_downloader.api import APIError
    engine, api, tmp = system
    api.file("disk:/song.mp3", media_type="audio", mime_type="audio/mpeg")
    api.fail[("disk:/song.mp3", "original")] = [APIError("Нет доступа", 403)]
    job = download(engine, tmp / "results", mode="fallback", file_types=["music"])
    assert job["errors"] == 1 and job["previews"] == 0
    assert api.media_calls == [("disk:/song.mp3", "original")]


def test_missing_filter_prunes_zip_and_inherits_previous_types(system):
    engine, api, tmp = system
    mixed(api)
    first = download(engine, tmp / "results", file_types=["photos"])
    old_photo = Path(first["root"]) / "nested/photo.jpg.preview.png"
    api.media_calls.clear()
    second = download(engine, first["root"], operation="missing", file_types=["music", "files"])
    assert second["files"] == second["originals"] == 2 and second["previews"] == 0
    assert old_photo.is_file()
    with zipfile.ZipFile(safe_join(second["root"], second["zip_path"])) as archive:
        assert "nested/music.mp3" in archive.namelist()
        assert "nested/document.pdf" in archive.namelist()
        assert not any("photo.jpg" in name or "clip.mp4" in name for name in archive.namelist())
    api.media_calls.clear()
    inherited = download(engine, second["root"], operation="missing")
    assert inherited["settings"]["file_types"] == ["files", "music"]
    assert inherited["existing"] == 2 and api.media_calls == []


def test_type_changed_after_scan_is_not_downloaded(system):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    job_id = engine.create({"destination": str(tmp / "results"), "whole_disk": True, "file_types": ["photos"]})
    wait_job(engine, job_id)
    api.file("disk:/one.bin", media_type="audio", mime_type="audio/mpeg")
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["counts"]["filtered"] == 1 and job["processed"] == 1 and job["percent"] == 100
    assert api.media_calls == [] and job["saved_bytes"] == 0


def test_legacy_non_media_preview_stays_local_but_is_excluded_from_new_zip(system):
    engine, api, tmp = system
    api.file("disk:/song.mp3")
    first = download(engine, tmp / "results", file_types=["photos"])
    previous = Path(first["root"]) / "song.mp3.preview.png"
    api.file("disk:/song.mp3", media_type="audio", mime_type="audio/mpeg")
    api.media_calls.clear()
    second = download(engine, first["root"], operation="missing", file_types=["music"])
    assert second["originals"] == 1 and second["previews"] == 0
    assert previous.is_file()
    with zipfile.ZipFile(safe_join(second["root"], second["zip_path"])) as archive:
        assert "song.mp3" in archive.namelist()
        assert "song.mp3.preview.png" not in archive.namelist()


@pytest.mark.parametrize("selection", [[], ["unknown"], "photos", [True]])
def test_invalid_type_selection_is_rejected(system, selection):
    engine, api, tmp = system
    with pytest.raises(ValueError, match="тип"):
        engine.validate_settings({"destination": str(tmp), "whole_disk": True, "file_types": selection})


def test_eta_known_bytes_unknown_previews_stall_and_phase_switch():
    moment = [0]
    eta = RemainingTime(lambda: moment[0])
    eta.start("downloading")
    moment[0] = 1
    eta.advance('original', 32768)
    assert eta.estimate("downloading", 32768, 0, 196608, 1) is None
    for step in (2, 3):
        moment[0] = step
        eta.advance('original', 32768)
    assert eta.estimate("downloading", 98304, 0, 196608, 1) == 6
    for step in (4, 5, 6):
        moment[0] = step
        eta.result(True)
    assert eta.estimate("downloading", 98304, 3, 10000, 9, 9) == 18
    moment[0] = 40
    assert eta.estimate("downloading", 20, 1, 10000, 9, 9) is None
    eta.start("packing", 0)
    moment[0] = 45
    assert eta.estimate("downloading", 20, 1, 80, 9) is None
    assert eta.estimate("packing", 20, 0, 80, 0) == 20


def test_eta_pause_excludes_paused_time(monkeypatch):
    from disk_downloader import engine as module
    moment = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: moment[0])
    control = Control()
    eta = control.remaining_time
    eta.start("downloading")
    for step in (1, 2, 3):
        moment[0] = step
        eta.advance('original', 32768)
    control.pause()
    moment[0] = 23
    assert eta.estimate("downloading", 98304, 0, 196608, 1) == 6
    control.resume()
    moment[0] = 24
    eta.advance('original', 32768)
    assert eta.estimate("downloading", 131072, 0, 196608, 1) == 6


def test_job_eta_accounts_for_active_bytes_and_zero_after_completion(system):
    engine, api, tmp = system
    api.file("disk:/one.jpg", b"x" * 1000)
    job_id = engine.create({"destination": str(tmp / "results"), "whole_disk": True})
    wait_job(engine, job_id)
    control = Control()
    engine.controls[job_id] = control
    engine.threads[job_id] = SimpleNamespace(is_alive=lambda: True)
    engine.store.update_job(job_id, phase="downloading")
    moment = [0]
    control.remaining_time = RemainingTime(lambda: moment[0])
    control.remaining_time.start("downloading")
    for step in (1, 2, 3):
        moment[0] = step
        control.remaining_time.advance('preview', 32768)
    control.transferred = 98304
    control.progress("disk:/one.jpg", 98304, 294912, "Загрузка превью", kind="preview", source_size=1000)
    assert engine.status(job_id)["eta_seconds"] == 6
    engine.store.update_job(job_id, phase="paused")
    assert engine.status(job_id)["eta_seconds"] is None
    engine.store.update_job(job_id, phase="completed")
    engine.threads[job_id] = SimpleNamespace(is_alive=lambda: False)
    assert engine.status(job_id)["eta_seconds"] == 0


@pytest.fixture(autouse=True)
def short_scan_pages(monkeypatch):
    monkeypatch.setattr('disk_downloader.scanner.PAGE_SIZE',200)
