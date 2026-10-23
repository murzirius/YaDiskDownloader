"""Regression checks for durable download results and optional caches."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

import pytest

from conftest import make_job, wait_job
from disk_downloader.paths import native


@pytest.mark.parametrize('attempts', [1, 2])
@pytest.mark.parametrize('mode,expected', [
    ('originals', {'original'}), ('fallback', {'original'}), ('previews', {'preview'}),
    ('both', {'original', 'preview'}),
])
def test_optional_cache_failure_preserves_committed_result(system, monkeypatch, attempts, mode, expected):
    engine, api, _ = system
    source = 'disk:/one.bin'
    if mode == 'originals':
        api.file(source, b'checked bytes', preview=False, media_type='document', mime_type='application/octet-stream')
    else:
        api.file(source, b'checked bytes')
    job_id = make_job(system, mode='fallback' if mode == 'originals' else mode)
    settings = engine.store.job(job_id)['settings']
    settings['attempts'] = attempts
    engine.store.update_job(job_id, settings=json.dumps(settings))
    original_db = engine.store.db
    failures = []

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql.startswith('INSERT OR REPLACE INTO verification_cache'):
                failures.append(sql)
                raise sqlite3.OperationalError('database is locked')
            return self.db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def failing_cache_db():
        with original_db() as db:
            yield Connection(db)

    monkeypatch.setattr(engine.store, 'db', failing_cache_db)
    engine.action(job_id, 'start')
    job = wait_job(engine, job_id)
    assert failures
    assert job['phase'] == 'completed', job
    assert job['errors'] == 0
    artifacts = list(engine.store.rows('SELECT * FROM artifacts WHERE job=?', (job_id,)))
    assert {row['type'] for row in artifacts} == expected
    assert api.media_calls == [(source, kind) for kind in sorted(expected)]
    assert job['originals'] == int('original' in expected)
    assert job['previews'] == int('preview' in expected)
    for row in artifacts:
        assert Path(native(Path(job['root'], row['local']))).read_bytes() == api.data[(source, row['type'])]
    media_files = [p for p in Path(job['root']).iterdir() if p.name.startswith('one')]
    assert len(media_files) == len(expected)


def test_optional_cache_does_not_hide_missing_or_substituted_file(system, monkeypatch):
    engine, _, tmp = system
    job = {'id': 'test', 'root': str(tmp)}
    with pytest.raises(OSError):
        engine.remember_file(job, 'missing.bin', 'abc')
    def substituted(*args):
        raise ValueError('unsafe path')
    monkeypatch.setattr('disk_downloader.engine.safe_join', substituted)
    with pytest.raises(ValueError, match='unsafe path'):
        engine.remember_file(job, 'any.bin', 'abc')
