import json
import zipfile
from pathlib import Path

from conftest import make_job, run_job, wait_job
from disk_downloader.paths import safe_join


def test_automatic_outputs_are_directly_in_import_folder(system):
    engine, api, _ = system
    api.file("disk:/Фото/photo.jpg")
    api.directory("disk:/Пустая")
    job = run_job(system)
    root = Path(job["root"])
    assert Path(job["zip_path"]).parent == Path(".")
    assert (root / "result-1.zip").is_file()
    assert {p.name for p in root.iterdir() if p.is_dir()} == {"Фото", "Пустая"}
    assert not (root / job["service"]).exists()
    assert not list(root.rglob("*.part"))
    for extension in ("csv", "json", "html", "txt"):
        assert (root / f"report-1.{extension}").is_file()
    with zipfile.ZipFile(safe_join(job["root"], job["zip_path"])) as archive:
        assert "Фото/photo.jpg.preview.png" in archive.namelist()
        assert "Пустая/" in archive.namelist()
        assert not any(name.endswith(".zip") or name.endswith(".part") for name in archive.namelist())
        assert json.loads(archive.read(job["service"] + "/reports/report.json")) == json.loads((root / "report-1.json").read_text(encoding="utf-8"))
    original_archive = (root / "result-1.zip").read_bytes()
    original_report = (root / "report-1.json").read_bytes()
    engine.action(job["id"], "pack")
    rebuilt = wait_job(engine, job["id"])
    assert rebuilt["zip_path"] == "result-2.zip"
    assert (root / "report-2.json").is_file()
    assert (root / "result-1.zip").read_bytes() == original_archive
    assert (root / "report-1.json").read_bytes() == original_report
    assert not (root / job["service"]).exists()


def test_output_names_preserve_remote_and_foreign_files(system):
    engine, api, _ = system
    sources = ["report-1.csv", "REPORT-1.JSON", "result-1.zip", "result-1.zip.part"]
    for name in sources:
        api.file("disk:/" + name, data=("source:" + name).encode(), preview=False)
    job_id = make_job(system, mode="fallback")
    root = Path(engine.status(job_id)["root"])
    foreign = root / "report-1.html"
    foreign.write_bytes(b"foreign-report")
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    assert job["phase"] == "completed"
    assert Path(job["zip_path"]).parent == Path(".")
    assert job["zip_path"] != "result-1.zip"
    for name in sources:
        assert (root / name).read_bytes() == ("source:" + name).encode()
    assert foreign.read_bytes() == b"foreign-report"
    with zipfile.ZipFile(safe_join(job["root"], job["zip_path"])) as archive:
        for name in sources:
            assert archive.read(name) == ("source:" + name).encode()
        html_name = job["service"] + "/reports/report.html"
        assert b"foreign-report" not in archive.read(html_name)
        assert archive.read(job["service"] + "/reports/report.csv") != (root / "report-1.csv").read_bytes()
