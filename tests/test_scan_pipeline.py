import shutil
import threading
import time
from pathlib import Path

import pytest

from conftest import Response, test_timeout, wait_job
from disk_downloader.engine import Engine
from disk_downloader.space_budget import HARD_FREE_BYTES, RESUME_FREE_BYTES, may_finish
from disk_downloader.store import Store
from disk_downloader.management import Management


def populate(api, count):
    for index in range(count):
        api.file(f'disk:/{index:04}.bin', data=b'data', preview=False, media_type='document')


def test_large_folder_fetches_pages_concurrently_but_saves_in_order(system, monkeypatch):
    from disk_downloader import scanner
    engine, api, root = system
    monkeypatch.setattr(scanner, 'PAGE_SIZE', 20)
    populate(api, 100)
    original = api.metadata
    lock = threading.Lock()
    active = peak = 0
    def metadata(path, settings=None, checkpoint=lambda:None, offset=0, limit=200, **kwargs):
        nonlocal active, peak
        if limit != 20:
            return original(path, settings, checkpoint, offset=offset, limit=limit, **kwargs)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.08 if offset == 20 else .02)
            return original(path, settings, checkpoint, offset=offset, limit=limit, **kwargs)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(api, 'metadata', metadata)
    job = engine.create({'destination': str(root/'output'), 'whole_disk': True})
    result = wait_job(engine, job)
    assert result['phase'] == 'ready' and result['files'] == 100
    assert 2 <= peak <= 4
    with engine.store.db() as db:
        assert db.execute("SELECT scan_offset,scanned FROM entries WHERE job=? AND path='disk:/'", (job,)).fetchone()[:] == (100, 1)
    assert sorted(offset for path, offset, limit in api.metadata_calls if limit == 20) == [0,20,40,60,80]


def test_server_short_pages_keep_actual_cursor(system, monkeypatch):
    from disk_downloader import scanner
    engine, api, root = system
    monkeypatch.setattr(scanner, 'PAGE_SIZE', 20)
    populate(api, 87)
    original = api.metadata
    def short(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('offset') == 20:
            result['_embedded']['items'] = result['_embedded']['items'][:7]
        return result
    monkeypatch.setattr(api, 'metadata', short)
    job = engine.create({'destination': str(root/'output'), 'whole_disk': True})
    result = wait_job(engine, job)
    assert result['files'] == 87 and not result['scan_errors']
    assert any(offset == 27 for _, offset, limit in api.metadata_calls if limit == 20)


def test_restart_and_new_missing_request_reuse_unfinished_page_cursor(system, monkeypatch):
    from disk_downloader import scanner
    engine, api, root = system
    monkeypatch.setattr(scanner, 'PAGE_SIZE', 20)
    populate(api, 100)
    original = api.metadata
    blocked = threading.Event()
    def metadata(path, settings=None, checkpoint=lambda:None, offset=0, limit=200, **kwargs):
        if limit == 20 and offset >= 40:
            blocked.set()
            while True:
                checkpoint()
                time.sleep(.01)
        return original(path, settings, checkpoint, offset=offset, limit=limit, **kwargs)
    monkeypatch.setattr(api, 'metadata', metadata)
    job = engine.create({'destination': str(root/'output'), 'whole_disk': True, 'mode':'fallback', 'create_zip':False})
    assert blocked.wait(test_timeout(10))
    deadline = time.monotonic() + test_timeout(10)
    while time.monotonic() < deadline:
        with engine.store.db() as db:
            cursor = db.execute("SELECT scan_offset FROM entries WHERE job=? AND path='disk:/'",(job,)).fetchone()[0]
        if cursor >= 40:
            break
        time.sleep(.01)
    assert cursor == 40
    engine.action(job, 'stop')
    stopped = wait_job(engine, job)
    assert not stopped['scan_complete']
    monkeypatch.setattr(api, 'metadata', original)
    api.metadata_calls.clear()
    restarted = Engine(Store(engine.store.filename), api)
    resumed = restarted.create({'operation':'missing', 'destination':stopped['root'], 'whole_disk':True, 'mode':'fallback', 'create_zip':False, 'auto_start':True})
    assert resumed == job
    result = wait_job(restarted, resumed)
    assert result['phase'] == 'completed' and result['files'] == result['processed'] == 100
    offsets = [offset for path, offset, limit in api.metadata_calls if path == 'disk:/' and limit == 20]
    assert offsets and min(offsets) == 40 and 0 not in offsets and 20 not in offsets


def test_auto_start_runs_scan_download_and_reports_without_extra_action(system):
    engine, api, root = system
    populate(api, 3)
    job = engine.create({'destination':str(root/'output'), 'whole_disk':True, 'mode':'fallback', 'auto_start':True, 'create_zip':False})
    result = wait_job(engine, job)
    assert result['phase'] == 'completed' and result['processed'] == 3
    assert (Path(result['root'])/'report.json').is_file()


def test_hard_floor_cannot_be_disabled_and_resume_requires_cleaned_space(system, monkeypatch):
    from types import SimpleNamespace
    from disk_downloader import destination
    # Windows/Python 3.11 can give consecutive probes the same clock tick.
    # Fresh free-space values must be recognized independently of timestamps.
    monkeypatch.setattr(destination, 'time', SimpleNamespace(monotonic=lambda: 100.0))
    engine, api, root = system
    populate(api, 1)
    sample = shutil.disk_usage(root)
    free = [HARD_FREE_BYTES - 1]
    monkeypatch.setattr(shutil, 'disk_usage', lambda path:type(sample)(10**11,10**11-free[0],free[0]))
    job = engine.create({'destination':str(root/'output'), 'whole_disk':True, 'auto_pause':False})
    paused = wait_job(engine, job, phases={'paused'})
    assert '500' in paused['pause_reason'] and not api.metadata_calls
    free[0] = HARD_FREE_BYTES + 1
    with pytest.raises(ValueError, match='1 ГБ'):
        engine.action(job, 'resume')
    free[0] = RESUME_FREE_BYTES
    engine.action(job, 'resume')
    assert wait_job(engine, job)['phase'] == 'ready'


def test_hard_floor_finishes_small_current_file_then_starts_no_new_file(system, monkeypatch):
    engine, api, root = system
    populate(api, 2)
    job = engine.create({'destination':str(root/'output'), 'whole_disk':True, 'mode':'fallback', 'workers':1, 'auto_pause':False, 'create_zip':False})
    ready = wait_job(engine, job)
    sample = shutil.disk_usage(root)
    free = [2*RESUME_FREE_BYTES]
    monkeypatch.setattr(shutil, 'disk_usage', lambda path:type(sample)(10**11,10**11-free[0],free[0]))
    original = api.media
    class Filling(Response):
        def iter_content(self, chunk_size):
            yield self.data[:1]
            free[0] = HARD_FREE_BYTES - 1024
            engine.controls[job].next_space_check = 0
            yield self.data[1:]
    def media(*args, **kwargs):
        response, session = original(*args, **kwargs)
        return Filling(response.data), session
    monkeypatch.setattr(api, 'media', media)
    engine.action(job, 'start')
    paused = wait_job(engine, job, phases={'paused'})
    assert (Path(ready['root'])/'0000.bin').read_bytes() == b'data'
    assert api.media_calls == [('disk:/0000.bin','original')]
    assert '500' in paused['pause_reason']
    engine.action(job, 'stop')
    wait_job(engine, job)


def test_finish_budget_is_shared_and_never_allows_large_or_unknown_transfers():
    assert may_finish({1:1024,2:2048}, HARD_FREE_BYTES-1)
    assert not may_finish({1:None}, HARD_FREE_BYTES-1)
    assert not may_finish({1:40*1024**2,2:40*1024**2}, HARD_FREE_BYTES-1)
    assert not may_finish({1:1024}, 1024)


def test_large_current_file_pauses_and_preserves_its_verified_partial(system, monkeypatch):
    import hashlib
    engine, api, root = system
    data = b'x' * 65536
    api.file('disk:/large.bin',data=data,preview=False,media_type='document')
    api.tree['disk:/large.bin']['size'] = 80*1024**2
    job = engine.create({'destination':str(root/'output'),'whole_disk':True,'mode':'fallback','workers':1,'auto_pause':False,'create_zip':False})
    wait_job(engine,job)
    sample = shutil.disk_usage(root)
    free = [2*RESUME_FREE_BYTES]
    monkeypatch.setattr(shutil,'disk_usage',lambda path:type(sample)(10**11,10**11-free[0],free[0]))
    class Filling(Response):
        def iter_content(self, chunk_size):
            yield data
            free[0] = HARD_FREE_BYTES-1
            engine.controls[job].next_space_check = 0
            yield data
    monkeypatch.setattr(api,'media',lambda *args,**kwargs:(Filling(data,headers={'Content-Length':str(80*1024**2)}),None))
    engine.action(job,'start')
    wait_job(engine,job,phases={'paused'})
    engine.action(job,'stop')
    result = wait_job(engine,job)
    with engine.store.db() as db:
        part = db.execute('SELECT local,size,sha256 FROM partials WHERE job=?',(job,)).fetchone()
    assert part is not None and part['size'] == len(data)
    assert (Path(result['root'])/part['local']).read_bytes() == data
    assert part['sha256'] == hashlib.sha256(data).hexdigest()


def test_scheduled_auto_start_prepares_then_queues_without_early_download(system):
    engine, api, root = system
    manager = Management(engine)
    populate(api, 2)
    job = engine.create({'destination':str(root/'output'), 'whole_disk':True, 'mode':'fallback', 'auto_start':True, 'run_at':'2099-01-01T00:00:00+00:00'})
    result = wait_job(engine, job)
    assert result['phase'] == 'ready' and not api.media_calls
    assert manager.armed and manager.queue_snapshot()['items'][0]['job'] == job
    manager.tick()
    assert not api.media_calls


def test_parallel_cached_checks_still_hash_changed_files_on_owner_thread(system, monkeypatch):
    import disk_downloader.engine as engine_module
    engine, api, root = system
    populate(api, 12)
    first = engine.create({'destination':str(root/'output'), 'whole_disk':True, 'mode':'fallback', 'auto_start':True, 'create_zip':False})
    done = wait_job(engine, first)
    (Path(done['root'])/'0003.bin').write_bytes(b'xxxx')
    original = engine._check_local_results
    def enable(job, control):
        control.cached_probe_pool = control.available_probe_pool
        return original(job,control)
    monkeypatch.setattr(engine,'_check_local_results',enable)
    hashed_threads = []
    original_hash = engine_module.file_hash
    def file_hash(*args, **kwargs):
        hashed_threads.append(threading.current_thread().name)
        return original_hash(*args,**kwargs)
    monkeypatch.setattr(engine_module,'file_hash',file_hash)
    missing = engine.create({'operation':'missing','destination':done['root'],'whole_disk':True,'mode':'fallback','create_zip':False})
    ready = wait_job(engine, missing)
    assert ready['phase'] == 'ready' and ready['existing'] == 11
    assert hashed_threads and not any(name.startswith('disk-verify') for name in hashed_threads)
    assert ready['verification']['hashed'] >= 1


def test_resume_interrupted_download_reuses_source_snapshot_for_finished_files(system, monkeypatch):
    engine, api, root = system
    populate(api, 2)
    job = engine.create({'destination':str(root/'output'),'whole_disk':True,'mode':'fallback','workers':1,'create_zip':False})
    wait_job(engine,job)
    original = api.media
    reached = threading.Event()
    def media(path, kind, settings, checkpoint=lambda:None):
        if path.endswith('0001.bin'):
            reached.set()
            while True:
                checkpoint()
                time.sleep(.01)
        return original(path,kind,settings,checkpoint)
    monkeypatch.setattr(api,'media',media)
    engine.action(job,'start')
    assert reached.wait(test_timeout(10))
    engine.action(job,'stop')
    wait_job(engine,job)
    monkeypatch.setattr(api,'media',original)
    api.metadata_calls.clear();api.media_calls.clear()
    engine.action(job,'resume')
    result = wait_job(engine,job)
    assert result['phase'] == 'completed'
    assert not any(path == 'disk:/0000.bin' for path,_,_ in api.metadata_calls)
    assert api.media_calls == [('disk:/0001.bin','original')]
