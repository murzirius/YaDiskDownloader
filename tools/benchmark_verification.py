"""Local, isolated verification benchmark; never uses a token or user files."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
import subprocess
import types
from contextlib import ExitStack
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 1000, 10000])
    parser.add_argument('--baseline-ref', help='Use the Engine from a trusted local Git revision')
    parser.add_argument('--stat-delay-ms', type=float, default=0, help='Simulate metadata latency only for disposable fixture files')
    options = parser.parse_args()
    sys.path.insert(0, str(options.source.resolve()))
    from disk_downloader.api import Credentials
    from disk_downloader.engine import Engine, Control, fingerprint
    if not 0 <= options.stat_delay_ms <= 100:
        parser.error('Metadata latency must be between 0 and 100 ms')
    if options.baseline_ref:
        baseline = types.ModuleType('disk_downloader._verification_baseline')
        code = subprocess.check_output(['git','show',options.baseline_ref+':disk_downloader/engine.py'],cwd=options.source)
        exec(compile(code,'baseline-engine.py','exec'),baseline.__dict__)
        Engine,Control = baseline.Engine,baseline.Control
    from disk_downloader.paths import file_signature, native
    from disk_downloader.store import Store
    from types import SimpleNamespace
    reports = []
    for size in options.sizes:
        with tempfile.TemporaryDirectory(prefix='yadisk-verification-benchmark-') as temporary:
            base = Path(temporary)
            root = base / 'files'
            root.mkdir()
            store = Store(base / 'state/jobs.sqlite3')
            engine = Engine(store, SimpleNamespace(credentials=Credentials()))
            settings = engine.validate_settings({'operation':'missing', 'destination':str(root), 'whole_disk':True, 'auto_pause':False})
            with store.db() as db:
                db.execute("INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES('bench',?,?,'service','ready','2026-10-18',1)", (json.dumps(settings),str(root)))
                for offset in range(0,size,200):
                    entries, artifacts, cache = [], [], []
                    for index in range(offset,min(size,offset+200)):
                        local = f'file-{index:07}.bin'
                        file = root / local
                        file.write_bytes(b'data')
                        sha = hashlib.sha256(b'data').hexdigest()
                        meta = {'path':'disk:/'+local,'name':local,'type':'file','size':4,'sha256':sha,'mime_type':'application/octet-stream'}
                        entries.append(('bench',meta['path'],'file',json.dumps(meta),local,'existing','existing_verified','not_needed',2))
                        artifacts.append(('bench',meta['path'],'original',local,4,sha,fingerprint(meta),None))
                        cache.append(('bench',local,json.dumps(file_signature(native(file),sha))))
                    db.executemany('INSERT INTO entries(job,path,kind,meta,local,status,original_status,preview_status,priority) VALUES(?,?,?,?,?,?,?,?,?)', entries)
                    db.executemany('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',artifacts)
                    db.executemany('INSERT INTO verification_cache VALUES(?,?,?)',cache)
            counts = {'connections':0,'selects':0,'commits':0}
            connect = store._connect
            def measured_connect():
                counts['connections'] += 1
                connection = connect()
                def trace(sql):
                    command = sql.split(None,1)[0].upper()
                    if command=='COMMIT': counts['commits']+=1
                    if command=='SELECT': counts['selects']+=1
                connection.set_trace_callback(trace)
                return connection
            store._connect = measured_connect
            job = store.job('bench')
            counts.update(connections=0,selects=0,commits=0)
            tracemalloc.start()
            cpu, wall = time.process_time(), time.perf_counter()
            with ExitStack() as stack:
                if options.stat_delay_ms:
                    import os
                    for name in ('stat','lstat'):
                        original = getattr(os,name)
                        def delayed(path,*args,_original=original,**kwargs):
                            if 'yadisk-verification-benchmark-' in str(path):
                                time.sleep(options.stat_delay_ms/1000)
                            return _original(path,*args,**kwargs)
                        stack.enter_context(patch.object(os,name,delayed))
                engine.check_local_results(job,Control())
            summary = store.summary('bench')
            elapsed, cpu_used = time.perf_counter()-wall, time.process_time()-cpu
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert summary['existing']==size and summary['originals']==size
            report = {'files':size,'seconds':round(elapsed,4),'cpu_seconds':round(cpu_used,4),'peak_python_bytes':peak,**counts}
            reports.append(report)
            print(json.dumps(report),flush=True)
    options.output.parent.mkdir(parents=True,exist_ok=True)
    options.output.write_text(json.dumps(reports,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
