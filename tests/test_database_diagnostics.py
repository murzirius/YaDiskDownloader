"""Background SQLite failures must retain useful, private-data-free evidence."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import make_job, run_job, wait_job
from disk_downloader.diagnostics import bundle, error_kind
from disk_downloader.engine import safe_error
from disk_downloader.request_errors import RequestErrors
from disk_downloader.sqlite_support import error_details


@pytest.mark.parametrize('message,reason', [
    ('no such table: private_table', 'missing_table'),
    ('no such column: private_column', 'missing_column'),
    ('ambiguous column name: private_column', 'ambiguous_column'),
    ('invalid uri authority: private-server', 'uri_authority'),
    ('near "private-value": syntax error', 'sql_syntax'),
    ('malformed JSON', 'invalid_json'),
    ('Nested write connections are not supported', 'nested_write'),
    ('private-path-and-token', 'unknown'),
    ("Could not decode to UTF-8 column 'meta' with text 'private'", 'text_decode'),
    ('Error while building row_cast_map', 'column_decode'),
])
def test_sqlite_reasons_never_export_exception_text(tmp_path, message, reason):
    error = sqlite3.OperationalError(message)
    recorder = RequestErrors(tmp_path)
    recorder.record('test', 'job_scan', error, phase='preparing')
    record = recorder.snapshot()[0]
    assert record['sqlite']['reason'] == reason
    assert 'private' not in json.dumps(record)
    assert 'private' not in safe_error(error)
    assert 'Диагностика' in safe_error(error)
    assert 'Посмотреть состав' in safe_error(error)
    assert 'до закрытия программы' in safe_error(error)
    assert 'ПК' in safe_error(error)
    assert error_kind(safe_error(error)) == 'local_database'


@pytest.mark.parametrize('code,reason', [
    (sqlite3.SQLITE_BUSY, 'busy'), (sqlite3.SQLITE_LOCKED, 'busy'),
    (sqlite3.SQLITE_IOERR_TRUNCATE, 'storage'), (sqlite3.SQLITE_CANTOPEN, 'storage'),
    (sqlite3.SQLITE_READONLY, 'storage'), (sqlite3.SQLITE_FULL, 'full'),
    (sqlite3.SQLITE_CORRUPT, 'corrupt'), (sqlite3.SQLITE_NOTADB, 'corrupt'),
])
def test_extended_sqlite_codes_have_an_actionable_message(code, reason):
    error = sqlite3.OperationalError('private')
    error.sqlite_errorcode = code
    error.sqlite_errorname = 'SQLITE_TEST'
    assert error_details(error)['reason'] == reason
    assert 'private' not in safe_error(error)
    assert 'ПК' in safe_error(error)


def test_missing_import_failure_is_in_diagnostics_before_any_file_scan(system, monkeypatch):
    engine, api, tmp = system
    api.file('disk:/kept.jpg')
    first = run_job(system)
    # Use the actual registered copy, regardless of its image extension.
    with engine.store.db() as db:
        local = db.execute('SELECT local FROM artifacts WHERE job=?', (first['id'],)).fetchone()[0]
    original = Path(first['root'], local)
    data = original.read_bytes()
    recorder = engine.request_errors = RequestErrors(tmp/'state')
    calls = len(api.metadata_calls)
    def broken(*args, **kwargs):
        with sqlite3.connect(':memory:') as db:
            db.execute('SELECT private_column FROM sqlite_master')
    monkeypatch.setattr('disk_downloader.engine.inherit', broken)
    job_id = engine.create({'destination':first['root'], 'operation':'missing'})
    failed = wait_job(engine, job_id)
    assert failed['phase'] == 'failed' and failed['can_resume']
    assert len(api.metadata_calls) == calls
    records = bundle(engine, job_id)['requests.json']
    assert len(records) == 1
    record = records[0]
    assert record['sqlite']['reason'] == 'missing_column'
    assert record['sqlite']['code'] == 'SQLITE_ERROR'
    assert record['operation'] == 'job_scan' and record['phase'] == 'preparing'
    assert record['request_id'] in failed['error']
    assert record['frames'][-1]['function'] == 'broken'
    assert 'private_column' not in json.dumps(records)
    assert str(tmp) not in json.dumps(records)
    assert original.read_bytes() == data
    assert engine.store.job(job_id)['settings']['_inherit_source']
    saved = (tmp/'state/application-errors.jsonl').read_text(encoding='utf-8')
    assert json.loads(saved)['request_id'] == record['request_id']


def test_activity_failure_and_failed_diagnostic_io_still_finish_job(system, monkeypatch):
    engine, api, tmp = system
    api.file('disk:/kept.jpg')
    job_id = make_job(system)
    engine.request_errors = RequestErrors(tmp/'state')
    @contextmanager
    def broken():
        with sqlite3.connect(':memory:') as db:
            db.execute('SELECT * FROM private_table')
        yield
    monkeypatch.setattr(engine.store, 'activity', broken)
    engine.action(job_id, 'start')
    failed = wait_job(engine, job_id)
    assert failed['phase'] == 'failed'
    assert engine.request_errors.snapshot()[0]['sqlite']['reason'] == 'missing_table'
    def cannot_record(*args, **kwargs):
        raise OSError('diagnostic folder unavailable')
    monkeypatch.setattr(engine.request_errors, 'record', cannot_record)
    engine.action(job_id, 'resume')
    failed = wait_job(engine, job_id)
    assert failed['phase'] == 'failed' and failed['can_resume']
    assert 'SQLITE_ERROR' in failed['error']
    assert 'diagnostic folder unavailable' not in failed['error']


def test_secondary_manifest_failure_does_not_hide_primary_database_error(system, monkeypatch):
    engine, api, tmp = system
    api.file('disk:/kept.jpg')
    job_id = make_job(system)
    engine.request_errors = RequestErrors(tmp/'state')
    def broken_download(*args, **kwargs):
        with sqlite3.connect(':memory:') as db:
            db.execute('SELECT * FROM private_table')
    def broken_manifest(*args, **kwargs):
        raise OSError('private export failure')
    monkeypatch.setattr(engine, 'download', broken_download)
    monkeypatch.setattr('disk_downloader.engine.save_import', broken_manifest)
    engine.action(job_id, 'start')
    failed = wait_job(engine, job_id)
    assert failed['phase'] == 'failed' and 'SQLITE_ERROR' in failed['error']
    records = engine.request_errors.snapshot()
    assert records[0]['request_id'] in failed['error']
    assert [item['operation'] for item in records] == ['job_download', 'job_manifest']
    assert 'private' not in json.dumps(records)
