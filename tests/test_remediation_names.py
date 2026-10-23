"""Component budgets cover every suffix added during publication."""
import sys
import os
from pathlib import Path

import pytest

from conftest import make_job, wait_job
from disk_downloader.paths import component, digest, suffix, native


@pytest.mark.parametrize('name', ['я'*119+'?', 'a'*240, '😀'*59+'?', 'x.'+'я'*160, 'CON', 'photo.jpg'])
def test_names_reserve_preview_and_repeated_collision_budget(name):
    value = component(name, 'disk:/'+name)
    assert len(value.encode('utf-8')) <= 240
    for index in range(10):
        proposed = value + '.preview.avif' if index == 0 else value
        value = suffix(proposed, digest('version'+str(index)))
        if index == 0:
            assert '.preview~' in value and value.endswith('.avif')
        assert len(value.encode('utf-8')) <= 255
        assert len(value.encode('utf-16-le')) <= 510


def test_truncated_names_keep_identity_suffix_distinct():
    name = 'я'*119+'?'
    values = [component(name, 'disk:/'+str(index)+'/'+name) for index in range(20)]
    assert len(set(values)) == 20
    assert all('~'+digest('disk:/'+str(index)+'/'+name) in value for index,value in enumerate(values))


@pytest.mark.parametrize('foreign_exists', [False, True])
def test_legacy_oversized_preview_allocation_is_preserved_and_replaced_safely(system, foreign_exists):
    if foreign_exists and os.name != 'nt':
        pytest.skip('Legacy UTF-8 oversized file exists only on Windows in this fixture')
    engine, api, _ = system
    source='disk:/short.jpg'
    api.file(source)
    job_id=make_job(system,mode='previews')
    job=engine.store.job(job_id)
    legacy='я'*125+'.preview.png'
    with engine.store.db() as db:
        db.execute('INSERT INTO names VALUES(?,?,?,?)',(job_id,'@preview:'+source,legacy,legacy.casefold()))
    old_file=Path(native(Path(job['root'],legacy)))
    if foreign_exists:
        old_file.write_bytes(b'preserve foreign previous name')
    engine.action(job_id,'start')
    complete=wait_job(engine,job_id)
    assert complete['phase']=='completed',complete
    row=next(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(job_id,)))
    assert len(Path(row['local']).name.encode('utf-8')) <= 255
    if foreign_exists:
        assert old_file.read_bytes()==b'preserve foreign previous name'
    with engine.store.db() as db:
        assert db.execute('SELECT local FROM names WHERE job=? AND identity=?',(job_id,'@preview:'+source)).fetchone()[0]==legacy


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Native Linux NAME_MAX integration')
def test_linux_native_long_original_preview_and_collisions(system):
    engine, api, _ = system
    name = 'я'*119+'?'
    api.file('disk:/'+name)
    job_id = make_job(system, mode='both')
    engine.action(job_id, 'start')
    job = wait_job(engine, job_id)
    assert job['phase'] == 'completed' and job['errors'] == 0
    rows = list(engine.store.rows('SELECT local FROM artifacts WHERE job=?',(job_id,)))
    assert len(rows) == 2
    for row in rows:
        assert len(Path(row['local']).name.encode('utf-8')) <= 255
        assert Path(job['root'], row['local']).is_file()


def test_legacy_version_preview_allocation_stays_portable(system):
    from disk_downloader.engine import fingerprint
    engine, api, _ = system
    source = 'disk:/short.jpg'
    api.file(source)
    job_id = make_job(system, mode='previews')
    job = engine.store.job(job_id)
    foreign = Path(native(Path(job['root'], 'short.jpg.preview.png')))
    foreign.write_bytes(b'foreign collision must survive')
    legacy = 'я'*125+'.preview.png'
    version_identity = '@version:preview:'+source+':'+fingerprint(api.tree[source])+':1'
    with engine.store.db() as db:
        for identity, local in [('@preview:'+source, legacy), (version_identity, legacy+'x')]:
            db.execute('INSERT INTO names VALUES(?,?,?,?)', (job_id, identity, local, local.casefold()))
    engine.action(job_id, 'start')
    complete = wait_job(engine, job_id)
    assert complete['phase'] == 'completed', complete
    artifact = next(engine.store.rows('SELECT local FROM artifacts WHERE job=?', (job_id,)))
    assert len(Path(artifact['local']).name.encode('utf-8')) <= 255, artifact['local']
    assert foreign.read_bytes() == b'foreign collision must survive'
    with engine.store.db() as db:
        assert db.execute('SELECT local FROM names WHERE job=? AND identity=?', (job_id, version_identity)).fetchone()[0] == legacy+'x'
