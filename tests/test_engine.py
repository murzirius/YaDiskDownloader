import csv
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from conftest import make_job, run_job, wait_job, test_timeout
from disk_downloader.api import APIError
from disk_downloader.engine import Engine
from disk_downloader.paths import native, safe_join
from disk_downloader.store import Store


def entries(engine, job_id):
    return list(engine.store.rows("SELECT * FROM entries WHERE job=? AND kind='file' ORDER BY path", (job_id,)))


def report(job):
    with zipfile.ZipFile(safe_join(job["root"], job["zip_path"])) as archive:
        name = next(n for n in archive.namelist() if n.endswith("/report.json"))
        return json.loads(archive.read(name))


def test_pagination_more_than_1000_and_overlap(system):
    engine, api, _ = system
    api.directory("disk:/Фото/2024/Пустая")
    for i in range(1107):
        api.file(f"disk:/Фото/2024/{i:04}.dat", preview=False)
    job_id = make_job(system, paths=["disk:/Фото", "disk:/Фото/2024", "disk:/Фото/2024/0000.dat"])
    job = engine.status(job_id)
    assert job["files"] == 1107
    assert job["folders"] == 3
    assert job["settings"]["paths"] == ["disk:/Фото"]
    offsets = [offset for path, offset, limit in api.metadata_calls if path == "disk:/Фото/2024" and limit == 1000]
    assert offsets == [0, 1000]
    assert len(entries(engine, job_id)) == 1107
    assert Path(job["root"], "Фото", "2024", "Пустая").is_dir()
    assert job["percent"] == 0


@pytest.mark.parametrize("mode,types", [("previews", {"preview"}), ("both", {"original", "preview"}), ("fallback", {"original"})])
def test_modes_whole_disk_and_zip_structure(system, mode, types):
    engine, api, _ = system
    api.file("disk:/Фото/2024/отпуск.jpg")
    api.file("disk:/Документы/документ.pdf")
    api.directory("disk:/Пустая")
    job = run_job(system, mode=mode, whole=True)
    assert job["phase"] == "completed"
    assert job["processed"] == job["files"] == 2
    assert job["percent"] == 100
    assert job["settings"]["whole_disk"]
    assert {kind for path, kind in api.media_calls} == types
    with zipfile.ZipFile(safe_join(job["root"], job["zip_path"])) as archive:
        assert "Пустая/" in archive.namelist()
        assert not any(n.endswith(".part") or n.endswith(".py") for n in archive.namelist())
        assert "Фото/2024/отпуск.jpg" in archive.namelist() if "original" in types else "Фото/2024/отпуск.jpg.preview.png" in archive.namelist()
        assert len([n for n in archive.namelist() if "/report." in n]) == 4
        for info in archive.infolist():
            if not info.is_dir():
                assert info.extract_version >= 45
    data = report(job)
    assert data["summary"]["files"] == 2
    rows = [r for r in data["entries"] if r["record_type"] == "file"]
    assert all({r["type"] for r in row["results"]} == types for row in rows)
    assert job["zip_done"] == job["zip_total"]
    assert job["zip_bytes"] == os.path.getsize(safe_join(job["root"], job["zip_path"]))


def test_fallback_after_all_original_retries_and_both_fail(system):
    engine, api, _ = system
    api.file("disk:/good.bin")
    api.file("disk:/fallback.bin")
    api.file("disk:/bad.bin", preview=False)
    for path in ("disk:/fallback.bin", "disk:/bad.bin"):
        api.fail[(path, "original")] = [APIError("Ошибка оригинала", 503), APIError("Ошибка оригинала", 503)]
    job = run_job(system, mode="fallback")
    assert job["phase"] == "completed_with_errors"
    assert job["processed"] == 3 and job["percent"] == 100
    assert job["originals"] == job["previews"] == job["replacements"] == 1
    assert job["errors"] == 1
    assert api.media_calls.count(("disk:/fallback.bin", "original")) == 2
    assert ("disk:/good.bin", "preview") not in api.media_calls
    rows = {r["source_path"]: r for r in report(job)["entries"]}
    assert rows["disk:/fallback.bin"]["original_attempt"] == "failed"
    assert rows["disk:/fallback.bin"]["fallback_to_preview"]
    assert rows["disk:/bad.bin"]["status"] == "error"
    engine.action(job["id"], "retry")
    retried = wait_job(engine, job["id"])
    assert retried["originals"] == 3
    assert retried["previews"] == 0
    assert retried["processed"] == 3


def test_independent_both_and_absent_preview(system):
    engine, api, _ = system
    api.file("disk:/one.bin")
    api.file("disk:/two.bin", preview=False)
    api.fail[("disk:/one.bin", "original")] = [APIError("Нет оригинала", 403), APIError("Нет оригинала", 403)]
    job = run_job(system, mode="both")
    assert job["previews"] == 1 and job["originals"] == 1
    assert job["errors"] == 0 and job["missing_previews"] == 1
    assert job["phase"] == "completed"
    assert all(row["error"] == "" for row in entries(engine, job["id"]))
    assert all(r["status"] == "partial" for r in entries(engine, job["id"]))


def test_previews_never_request_original_unknown_estimate(system):
    engine, api, _ = system
    api.file("disk:/image.raw")
    api.file("disk:/no-preview.exe", preview=False)
    job_id = make_job(system)
    estimate = engine.status(job_id)["estimate"]
    assert estimate["known_bytes"] == 0 and estimate["unknown_sizes"] == 1
    assert estimate["original_bytes"] > 0
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert not any(kind == "original" for _, kind in api.media_calls)
    assert job["missing_previews"] == 1
    assert entries(engine, job_id)[1]["status"] == "no_preview"


def test_restart_revalidates_and_skips_verified_then_source_change(system):
    engine, api, tmp = system
    api.file("disk:/file.bin", b"version1")
    job = run_job(system, mode="fallback")
    calls = len(api.media_calls)
    restarted = Engine(Store(tmp / "state" / "jobs.sqlite3"), api)
    restarted.action(job["id"], "resume")
    again = wait_job(restarted, job["id"])
    assert len(api.media_calls) == calls
    assert again["existing"] == 1
    api.file("disk:/file.bin", b"version2-changed")
    restarted.action(job["id"], "resume")
    changed = wait_job(restarted, job["id"])
    assert len(api.media_calls) == calls + 1
    rows = report(changed)["entries"]
    row = next(r for r in rows if r["record_type"] == "file")
    actual = row["results"][0]["local"]
    assert actual != "file.bin"
    assert Path(job["root"], "file.bin").read_bytes() == b"version1"
    assert Path(job["root"], actual).read_bytes() == b"version2-changed"


def test_pause_resume_stop_and_partial_archive(system):
    engine, api, _ = system
    for i in range(6):
        api.file(f"disk:/{i}.bin")
    job_id = make_job(system, mode="both")
    api.block = threading.Event()
    api.delay = 1
    engine.action(job_id, "start")
    assert api.block.wait(test_timeout(10))
    engine.action(job_id, "pause")
    paused = engine.status(job_id)
    assert paused["phase"] == "paused" and paused["processed"] == 0
    engine.action(job_id, "resume")
    engine.action(job_id, "stop")
    stopped = wait_job(engine, job_id)
    assert stopped["phase"] == "stopped" and stopped["cancelled"] == 6
    assert stopped["originals"] == 0
    assert not list(Path(stopped["root"]).rglob("*.part"))
    engine.action(job_id, "pack")
    partial = wait_job(engine, job_id)
    assert partial["phase"] == "stopped" and partial["zip_path"]
    assert report(partial)["summary"]["cancelled"] == 6
    api.delay = 0
    engine.action(job_id, "resume")
    resumed = wait_job(engine, job_id)
    assert resumed["phase"] == "completed" and resumed["processed"] == 6


def test_corrupt_original_and_preview_are_not_completed(system):
    engine, api, _ = system
    api.file("disk:/bad.dat")
    api.data[("disk:/bad.dat", "original")] = b"wrong"
    api.data[("disk:/bad.dat", "preview")] = b"<html>not an image</html>"
    job = run_job(system, mode="both")
    assert job["errors"] == 1 and job["saved_bytes"] == 0, job
    assert not list(Path(job["root"]).rglob("*.part"))
    assert not list(engine.store.rows("SELECT * FROM artifacts WHERE job=?", (job["id"],)))


def test_foreign_files_and_windows_name_collisions(system):
    engine, api, _ = system
    names = ["Test.txt", "test.txt", "CON", "trailing. ", "x:y?.jpg", "x_y_.jpg", "a.jpg", "a.jpg.preview.png", "a" * 260 + ".jpg"]
    for name in names:
        api.file("disk:/Folder/" + name)
    job_id = make_job(system, mode="both")
    root = engine.status(job_id)["root"]
    Path(root, "Folder", "Test.txt").write_bytes(b"foreign")
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["errors"] == 0
    assert Path(root, "Folder", "Test.txt").read_bytes() == b"foreign"
    artifacts = list(engine.store.rows("SELECT * FROM artifacts WHERE job=?", (job_id,)))
    assert len({r["local"].casefold() for r in artifacts}) == 2 * len(names)
    for row in artifacts:
        # Test roots can exceed MAX_PATH too; inspect the same native path used
        # by downloads, rather than treating a Windows path limit as a lost file.
        assert Path(native(Path(root, row["local"]))).is_file()
        assert len(Path(row["local"]).name.encode("utf-16-le")) <= 510


@pytest.mark.parametrize("formats", [["csv"], ["json", "html"], ["txt"], ["csv", "json", "html", "txt"]])
def test_selected_reports_and_escaping(system, formats):
    engine, api, _ = system
    api.file("disk:/<script>alert(1)</script>.jpg")
    api.file("disk:/=SUM(1).txt", preview=False)
    job = run_job(system, reports=formats)
    with zipfile.ZipFile(safe_join(job["root"], job["zip_path"])) as archive:
        report_names = [n for n in archive.namelist() if "/report." in n]
        assert {n.rsplit(".", 1)[1] for n in report_names} == set(formats)
        assert not any(n.endswith("report.md") for n in archive.namelist())
        if "html" in formats:
            data = archive.read(next(n for n in report_names if n.endswith(".html"))).decode()
            assert "<script>" not in data and "&lt;script&gt;" in data
        if "csv" in formats:
            data = archive.read(next(n for n in report_names if n.endswith(".csv")))
            assert data.startswith(b"\xef\xbb\xbf")
            rows = list(csv.reader(data.decode("utf-8-sig").splitlines(), delimiter=";"))
            assert len(rows) >= 4
        for name in report_names:
            assert b"fake-token-for-tests" not in archive.read(name)
            assert b"transient-link" not in archive.read(name)
    assert b"fake-token-for-tests" not in Path(engine.store.filename).read_bytes()


def test_scan_failure_retained_and_retry(system):
    engine, api, _ = system
    api.file("disk:/a.bin")
    job_id = make_job(system, paths=["disk:/missing", "disk:/a.bin"])
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["scan_errors"] == 1 and job["phase"] == "completed_with_errors"
    assert any(r["record_type"] == "scan_error" for r in report(job)["entries"])
    api.file("disk:/missing")
    engine.action(job_id, "retry")
    retried = wait_job(engine, job_id)
    assert retried["scan_errors"] == 0 and retried["files"] == 2


def test_disk_full_preserves_other_results(system, monkeypatch):
    engine, api, _ = system
    api.file("disk:/one.bin")
    api.file("disk:/two.bin")
    original = engine.transfer

    def transfer(job, entry, kind, meta, control):
        if entry["path"] == "disk:/one.bin":
            raise OSError(28, "disk full")
        return original(job, entry, kind, meta, control)

    monkeypatch.setattr(engine, "transfer", transfer)
    job = run_job(system)
    assert job["errors"] == 1 and job["previews"] == 1
    assert "Недостаточно места" in entries(engine, job["id"])[0]["error"]


def test_interrupted_state_and_token_not_restored(system):
    engine, api, tmp = system
    api.file("disk:/one.bin")
    job_id = make_job(system)
    engine.store.update_job(job_id, phase="downloading")
    engine.store.update_entry(job_id, "disk:/one.bin", status="running")
    restored = Store(tmp / "state" / "jobs.sqlite3")
    assert restored.job(job_id)["phase"] == "interrupted"
    assert list(restored.rows("SELECT status FROM entries WHERE job=? AND kind='file'", (job_id,)))[0]["status"] == "pending"
