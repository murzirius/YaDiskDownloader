import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import run_job, wait_job
from disk_downloader.api import APIError
from disk_downloader.engine import Engine
from disk_downloader.folder_usage import FolderUsage
from disk_downloader.migration import migrate, candidates, legacy_lock
from disk_downloader.paths import file_signature
from disk_downloader.store import Store
from disk_downloader.web import create_app


def missing(system, old):
    engine = system[0]
    job_id = engine.create({"operation": "missing", "destination": old["root"], "whole_disk": True, "create_zip": False})
    return wait_job(engine, job_id)


def test_known_original_counts_immediately_and_is_not_requested_again(system):
    engine, api, _ = system
    api.file("disk:/local.jpg", b"original")
    first = run_job(system, mode="fallback")
    api.file("disk:/new.jpg", b"new-original")
    api.media_calls.clear()
    before = missing(system, first)
    assert before["existing"] == before["processed"] == before["previous_results"] == 1
    assert before["estimate"]["known_bytes"] == len(b"new-original")
    engine.action(before["id"], "start")
    after = wait_job(engine, before["id"])
    assert after["phase"] == "completed" and after["processed"] == 2
    assert api.media_calls == [("disk:/new.jpg", "original")]


def test_modified_file_with_restored_mtime_is_not_trusted(system):
    engine, api, _ = system
    api.file("disk:/one.jpg", b"correct")
    first = run_job(system, mode="fallback")
    filename = Path(first["root"]) / "one.jpg"
    info = filename.stat()
    signature = file_signature(filename, "hash")
    filename.write_bytes(b"damaged")
    os.utime(filename, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert signature != file_signature(filename, "hash")
    before = missing(system, first)
    assert before["existing"] == 0
    api.media_calls.clear()
    engine.action(before["id"], "start")
    after = wait_job(engine, before["id"])
    assert after["phase"] == "completed"
    assert api.media_calls == [("disk:/one.jpg", "original")]
    assert filename.read_bytes() == b"damaged"


def test_legacy_signature_is_hashed_and_counted_before_start(system):
    engine, api, _ = system
    api.file('disk:/legacy.jpg')
    first = run_job(system, mode='fallback')
    with engine.store.db() as db:
        row = db.execute('SELECT local,signature FROM verification_cache WHERE job=?',(first['id'],)).fetchone()
        db.execute('UPDATE verification_cache SET signature=? WHERE job=? AND local=?',(json.dumps(json.loads(row['signature'])[:6]),first['id'],row['local']))
    before = missing(system,first)
    assert before['previous_results'] == 1
    assert before['existing'] == 1
    engine.action(before['id'],'start')
    after = wait_job(engine,before['id'])
    assert after['existing'] == 1


def test_file_removed_after_estimate_is_downloaded_without_false_existing(system):
    engine, api, _ = system
    api.file("disk:/one.jpg")
    first = run_job(system, mode="fallback")
    before = missing(system, first)
    assert before["existing"] == 1
    Path(first["root"], "one.jpg").unlink()
    api.media_calls.clear()
    engine.action(before["id"], "start")
    after = wait_job(engine, before["id"])
    assert after["phase"] == "completed" and after["originals"] == 1
    assert api.media_calls == [("disk:/one.jpg", "original")]


def test_recovered_format_has_no_error_but_total_failure_and_os_error_do(system):
    engine, api, tmp = system
    api.file("disk:/fallback.jpg")
    api.fail[("disk:/fallback.jpg", "original")] = [APIError("HTTP 403", 403)] * 2
    recovered = run_job(system, mode="fallback")
    assert recovered["errors"] == 0 and recovered["phase"] == "completed"
    events = list(engine.store.rows("SELECT category,level FROM journal_events WHERE job=?", (recovered["id"],)))
    assert not any(row["category"] == "errors" and row["level"] == "error" for row in events)
    app = create_app(tmp / "state", api)
    client = app.test_client()
    base = 'http://127.0.0.1:8766'
    client.get('/',base_url=base)
    with client.session_transaction(base_url=base) as session:
        headers = {'X-CSRF-Token':session['csrf']}
    assert client.get('/api/jobs/'+recovered['id']+'/errors',base_url=base,headers=headers).json['items'] == []
    with engine.store.db() as db:
        row = db.execute("SELECT error FROM entries WHERE job=? AND kind='file'", (recovered['id'],)).fetchone()
    assert row[0] == ""
    api.file("disk:/both-failed.jpg", preview=False)
    api.fail[("disk:/both-failed.jpg", "original")] = [APIError("HTTP 500", 500)] * 2
    failed = run_job(system, mode="fallback")
    assert failed['errors'] == 1 and failed['phase'] == 'completed_with_errors'


def test_fixed_workers_even_if_old_profile_requests_adaptive(system):
    engine, api, tmp = system
    api.file("disk:/one.jpg")
    job_id = engine.create({"destination": str(tmp/'new'), "whole_disk": True, "workers": 8, "adaptive_workers": True})
    ready = wait_job(engine, job_id)
    assert ready['settings']['adaptive_workers'] is False
    assert ready['workers_status']['current'] == ready['workers_status']['maximum'] == 8
    assert engine.controls[job_id].adaptive is None


def test_preparation_batches_database_work(system, monkeypatch):
    engine, api, tmp = system
    for i in range(2500):
        api.file(f"disk:/Nested/Directory/{i:05}.jpg")
    job_id = engine.create({"destination": str(tmp/'large'), "whole_disk": True})
    ready = wait_job(engine, job_id, timeout=25)
    assert ready['phase'] == 'ready'
    with engine.store.db() as db:
        db.execute("UPDATE entries SET local=NULL WHERE job=?", (job_id,))
        db.execute("DELETE FROM names WHERE job=? AND identity NOT LIKE '@%'", (job_id,))
    count = 0
    original = engine.store.db
    @contextmanager
    def counted():
        nonlocal count
        count += 1
        with original() as db:
            yield db
    monkeypatch.setattr(engine.store, 'db', counted)
    engine.prepare_paths(job_id)
    assert count < 40, count
    assert next(engine.store.rows("SELECT COUNT(*) AS total FROM entries WHERE job=? AND local IS NULL", (job_id,)))['total'] == 0


def test_folder_size_includes_previously_saved_and_unregistered_content(tmp_path):
    root = tmp_path/'results'
    root.mkdir()
    (root/'old.zip').write_bytes(b'x'*1234)
    (root/'Photos').mkdir()
    (root/'Photos'/'old.jpg').write_bytes(b'x'*5678)
    usage = FolderUsage()
    first = usage.snapshot(root)
    assert first['scanning']
    until = time.monotonic()+5
    while time.monotonic() < until:
        snapshot = usage.snapshot(root)
        if not snapshot['scanning']:
            break
        time.sleep(.01)
    assert snapshot['bytes'] == 6912


def test_migration_preserves_jobs_profiles_and_never_replaces_newer_job(system):
    engine, api, tmp = system
    api.file("disk:/one.jpg")
    old = run_job(system, mode="fallback")
    with engine.store.db() as db:
        db.execute("INSERT INTO profiles VALUES(?,?)", ('profile', json.dumps(old['settings'])))
    central = tmp/'central'
    result = migrate(central, [tmp/'state'])
    assert result == {'jobs':1,'failed_sources':0}
    target = Store(central/'jobs.sqlite3')
    assert target.job(old['id'])['root'] == old['root']
    assert len(list(target.rows('SELECT * FROM profiles'))) == 1
    assert len(list(target.rows('SELECT * FROM artifacts WHERE job=?',(old['id'],)))) == 1
    assert Path(tmp/'state/jobs.sqlite3').is_file()
    target.update_job(old['id'], error='newer state')
    assert migrate(central,[tmp/'state'])['jobs'] == 0
    assert Store(central/'jobs.sqlite3').job(old['id'])['error'] == 'newer state'


def test_migration_skips_running_legacy_and_retries_later(system):
    engine, api, tmp = system
    api.file('disk:/one.jpg')
    job = run_job(system, mode='fallback')
    with legacy_lock(tmp/'state'):
        result = migrate(tmp/'central',[tmp/'state'])
    assert result['jobs'] == 0 and result['failed_sources'] == 1
    assert migrate(tmp/'central',[tmp/'state'])['jobs'] == 1


def test_migration_marks_active_jobs_interrupted_and_restores_queue(system):
    engine, api, tmp = system
    api.file('disk:/one.jpg')
    job = run_job(system, mode='fallback')
    engine.store.update_job(job['id'],phase='downloading')
    engine.store.update_entry(job['id'],'disk:/one.jpg',status='running')
    assert migrate(tmp/'central',[tmp/'state'])['jobs'] == 1
    target=Store(tmp/'central/jobs.sqlite3')
    assert target.job(job['id'])['phase'] == 'interrupted'
    assert next(target.rows('SELECT status FROM entries WHERE job=?',(job['id'],)))['status'] == 'pending'
