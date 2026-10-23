"""Measured hot paths and concurrency policy regressions."""
import json
from pathlib import Path
import time
import hashlib
import pytest

from conftest import make_job
from disk_downloader.engine import Control
from disk_downloader.maintenance import Maintenance
from disk_downloader.rate_limit import RateLimiter
from disk_downloader.reports import write_reports
from disk_downloader.work_policy import Policy


def test_cleanup_never_resolves_unrelated_saved_root(system, monkeypatch):
    engine, api, tmp = system
    api.file('disk:/one.bin')
    job_id = make_job(system)
    job = engine.store.job(job_id)
    unrelated = str(tmp / 'unavailable-old-drive')
    with engine.store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('old', json.dumps(job['settings']), unrelated, 'service', 'stopped', '2020'))
    import disk_downloader.maintenance as module
    original = module.root_key
    def checked(value):
        assert str(value) != unrelated, 'cleanup accessed an unrelated drive'
        assert not engine.lock._is_owned(), 'filesystem resolution under global lock'
        return original(value)
    monkeypatch.setattr(module, 'root_key', checked)
    assert Maintenance(engine).cleanup_preview(job['root'])['items'] == []


def test_cleanup_preview_file_probe_does_not_hold_control_lock(system, monkeypatch):
    engine, api, _ = system
    api.file('disk:/one.bin')
    job_id = make_job(system)
    root = engine.store.job(job_id)['root']
    local = '.disk-abcdefgh.part'
    Path(root, local).write_bytes(b'only synthetic temporary bytes')
    from disk_downloader.service_outputs import root_key
    with engine.store.db() as db:
        db.execute('INSERT INTO temporary_files VALUES(?,?,?,?)', (root_key(root), local, 'report', time.time()-7200))
    import disk_downloader.maintenance as module
    original = module.file_signature
    def checked(*args):
        assert not engine.lock._is_owned(), 'preview file IO under global lock'
        return original(*args)
    monkeypatch.setattr(module, 'file_signature', checked)
    result = Maintenance(engine).cleanup_preview(str(Path(root) / '.'))
    assert [row['local'] for row in result['items']] == [local]


def test_reports_resolve_root_once_and_keep_identical_outputs(system, monkeypatch):
    engine, api, _ = system
    for index in range(250):
        api.file(f'disk:/{index:04}.bin')
    job_id = make_job(system, reports=['csv', 'json'])
    import disk_downloader.reports as module
    calls = []
    original = module.root_key
    def counted(root):
        calls.append(root)
        return original(root)
    monkeypatch.setattr(module, 'root_key', counted)
    paths = write_reports(engine.store, job_id)
    assert len(calls) <= 3, f'{len(calls)} repeated root resolutions'
    root = Path(engine.store.job(job_id)['root'])
    contents = {path: (root / path).read_bytes() for path in paths}
    calls.clear()
    assert write_reports(engine.store, job_id) == paths
    assert len(calls) <= 3
    assert {path: (root / path).read_bytes() for path in paths} == contents


def test_workers_change_preserves_quiet_effective_rate(system):
    engine, api, _ = system
    api.file('disk:/one.bin')
    job_id = make_job(system)
    settings = engine.store.job(job_id)['settings']
    settings.update(speed_limit_mib=10, quiet_hours={'enabled': True, 'start': 0, 'end': 0,
                    'speed_limit_mib': 1, 'mute': True})
    engine.store.update_job(job_id, settings=json.dumps(settings))
    control = engine.controls[job_id] = Control()
    control.rate_limiter = RateLimiter(10)
    control.policy = Policy(settings, control)
    control.policy.check()
    assert control.rate_limiter.rate == 1024 * 1024
    revision = control.rate_limiter.revision
    engine.live_limits(job_id, {'workers': 4})
    assert control.rate_limiter.rate == 1024 * 1024
    assert control.rate_limiter.revision == revision
    engine.live_limits(job_id, {'speed_limit_mib': 0})
    assert control.rate_limiter.rate == 1024 * 1024
    control.policy.settings['quiet_hours']['enabled'] = False
    control.policy.next = 0
    control.policy.check()
    assert control.rate_limiter.rate == 0


def test_cleanup_preserves_reference_from_legacy_root_without_probing_it(system, monkeypatch):
    engine, api, tmp = system
    api.file('disk:/one.bin')
    job_id = make_job(system)
    job = engine.store.job(job_id)
    root = job['root']
    local = '.disk-legacy00.part'
    data = b'preserve even a conservatively matched legacy reference'
    Path(root, local).write_bytes(data)
    from disk_downloader.service_outputs import root_key
    with engine.store.db() as db:
        db.execute('INSERT INTO temporary_files VALUES(?,?,?,?)', (root_key(root), local, 'download', time.time()-7200))
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('legacy', json.dumps(job['settings']), str(tmp / 'old-alias'), 'service', 'stopped', '2020'))
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)', ('legacy', 'disk:/one.bin', 'original',
                   local, len(data), hashlib.sha256(data).hexdigest(), 'stamp', None))
    assert Maintenance(engine).cleanup_preview(root)['items'] == []
    assert Path(root, local).read_bytes() == data


def test_policy_transitions_keep_current_normal_and_quiet_rates(monkeypatch):
    import disk_downloader.work_policy as module
    active = [False]
    monkeypatch.setattr(module, 'quiet', lambda settings: active[0])
    control = Control()
    policy = Policy({'speed_limit_mib': 10, 'quiet_hours': {'speed_limit_mib': 1}}, control)
    policy.check()
    assert control.rate_limiter.rate == 10 * 1024 * 1024
    policy.update_speed(3)
    assert control.rate_limiter.rate == 3 * 1024 * 1024
    active[0] = True
    policy.next = 0
    policy.check()
    policy.update_speed(0)
    assert control.rate_limiter.rate == 1024 * 1024
    active[0] = False
    policy.next = 0
    policy.check()
    assert control.rate_limiter.rate == 0


@pytest.mark.parametrize('operation', ['action', 'limits'])
def test_history_restore_guard_blocks_job_mutation(system, operation):
    engine, api, _ = system
    api.file('disk:/one.bin')
    job_id = make_job(system)
    before = engine.store.job(job_id)
    engine.maintenance_busy = True
    try:
        with pytest.raises(ValueError, match='восстанавливает историю'):
            if operation == 'action':
                engine.action(job_id, 'stop')
            else:
                engine.live_limits(job_id, {'workers': 4})
        assert engine.store.job(job_id) == before
    finally:
        engine.maintenance_busy = False
