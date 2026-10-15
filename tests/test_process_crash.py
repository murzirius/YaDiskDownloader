import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import FakeAPI, wait_job, test_timeout
from disk_downloader.engine import Engine
from disk_downloader.store import Store
from disk_downloader.recovery import recover_publications


@pytest.mark.parametrize('phase',['scan','download','publish','reports','zip'])
def test_abrupt_process_termination_preserves_confirmed_results(tmp_path,phase):
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('crash_worker.py')),str(tmp_path),phase],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        due=time.monotonic()+test_timeout(30)
        while time.monotonic()<due and not (tmp_path/'reached').is_file():
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1].decode(errors='replace'))
            time.sleep(.05)
        assert (tmp_path/'reached').is_file(),'Child did not reach crash checkpoint'
        process.terminate();process.wait(timeout=5)
        job_id=(tmp_path/'job').read_text(encoding='ascii')
        store=Store(tmp_path/'state/jobs.sqlite3')
        job=store.job(job_id)
        assert job['phase']=='interrupted'
        assert not list(store.rows("SELECT * FROM entries WHERE status='running'"))
        recover_publications(store)
        api=FakeAPI();api.file('disk:/one.bin',data=b'x'*(8*1024*1024),preview=False)
        base_media=api.media
        def fast_media(*args,**kwargs):
            response,session=base_media(*args,**kwargs)
            response.chunks=1024*1024
            return response,session
        api.media=fast_media
        if phase=='scan':
            for n in range(450):api.file('disk:/item-'+str(n)+'.bin',data=b'item',preview=False)
        if phase=='download':
            parts=list(store.rows('SELECT size FROM partials'))
            assert parts and parts[0]['size']>=4*1024*1024
        if phase=='publish':
            assert list(store.rows('SELECT * FROM artifacts'))
        if phase=='zip':
            assert not list(Path(job['root']).glob('*.zip'))
        engine=Engine(store,api)
        engine.action(job_id,'resume',{'workers':20,'timeout':60})
        result=wait_job(engine,job_id,timeout=90)
        assert result['phase']=='completed',result
        assert (Path(result['root'])/'one.bin').read_bytes()==b'x'*(8*1024*1024)
        assert json.loads((Path(result['root'])/'report.json').read_text(encoding='utf-8'))['summary']['phase']=='completed'
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=5)
