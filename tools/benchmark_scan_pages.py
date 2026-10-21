"""Compare folder-page scheduling offline; never opens a real account or result folder."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from disk_downloader import scanner
from disk_downloader.api import Credentials
from disk_downloader.engine import Control, Engine
from disk_downloader.store import Store


def benchmark(reference, files, delay):
    old = types.ModuleType('disk_downloader._scan_baseline')
    code = subprocess.check_output(['git','show',reference+':disk_downloader/scanner.py'],cwd=ROOT)
    exec(compile(code,'baseline-scanner.py','exec'),old.__dict__)
    items = [{'path':f'disk:/{index:07}.bin','name':f'{index:07}.bin','type':'file','size':1} for index in range(files)]
    results = {}
    for label, module in [('before',old),('after',scanner)]:
        class API:
            def __init__(self):
                self.credentials = Credentials()
                self.lock = threading.Lock()
                self.calls = self.active = self.peak = 0
            def metadata(self,path,settings,checkpoint,offset=0,limit=1000):
                checkpoint()
                with self.lock:
                    self.calls += 1; self.active += 1; self.peak = max(self.peak,self.active)
                try:
                    time.sleep(delay)
                    checkpoint()
                    return {'path':'disk:/','name':'Disk','type':'dir','_embedded':{'items':copy.deepcopy(items[offset:offset+limit]),'total':files}}
                finally:
                    with self.lock:self.active -= 1
        with tempfile.TemporaryDirectory(prefix='yadisk-page-benchmark-') as folder:
            root = Path(folder)
            store = Store(root/'state.sqlite3')
            api = API(); engine = Engine(store,api)
            settings = engine.validate_settings({'destination':str(root),'whole_disk':True})
            with store.db() as db:
                db.execute("INSERT INTO jobs(id,settings,root,service,phase,started) VALUES('bench',?,?,'service','scanning','2026-01-01')",(json.dumps(settings),str(root)))
                store.insert_entry(db,'bench',{'path':'disk:/','name':'Disk','type':'dir'},settings)
            wall,cpu = time.perf_counter(),time.process_time()
            with store.activity():module.scan_pages(engine,'bench',settings,Control())
            elapsed,used = time.perf_counter()-wall,time.process_time()-cpu
            with store.db() as db:
                assert db.execute("SELECT COUNT(*) FROM entries WHERE job='bench' AND kind='file'").fetchone()[0] == files
                assert db.execute("SELECT scan_offset,scanned FROM entries WHERE job='bench' AND path='disk:/'").fetchone()[:] == (files,1)
                assert not db.execute('SELECT 1 FROM scan_errors').fetchone()
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            results[label] = {'seconds':round(elapsed,4),'cpu_seconds':round(used,4),'requests':api.calls,'parallel_requests':api.peak,'verified_files':files}
    return {'files':files,'simulated_request_latency_ms':delay*1000,'real_yandex_account':False,'results':results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-ref',required=True)
    parser.add_argument('--files',type=int,default=55000)
    parser.add_argument('--latency-ms',type=float,default=100)
    args = parser.parse_args()
    if not 1 <= args.files <= 100000 or not 0 <= args.latency_ms <= 1000:parser.error('Fixture size or latency is out of bounds')
    print(json.dumps(benchmark(args.baseline_ref,args.files,args.latency_ms/1000),indent=2))
