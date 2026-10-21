"""Compare history import against a Git revision using disposable metadata only."""
import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from disk_downloader import import_state
from disk_downloader.store import Store
from disk_downloader.engine import Engine
from disk_downloader.api import Credentials,DiskAPI


def benchmark(reference,files=2000,service_records=6000,runs=3):
    code=subprocess.check_output(['git','show',reference+':disk_downloader/import_state.py'],cwd=ROOT)
    baseline=types.ModuleType('disk_downloader._benchmark_import_baseline')
    exec(compile(code,'baseline-import-state.py','exec'),baseline.__dict__)
    results={}
    with tempfile.TemporaryDirectory(prefix='yadisk-import-benchmark-') as directory:
        folder=Path(directory);root=folder/'results';(root/'files').mkdir(parents=True)
        source=Store(folder/'source.sqlite3')
        settings=Engine(source,DiskAPI(Credentials())).validate_settings({'destination':str(root),'whole_disk':True,'mode':'fallback'})
        def add_job(store,identity):
            with store.db() as db:
                db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES(?,?,?,?,?,?,1)',(identity,json.dumps(settings),str(root),'service','completed','2026-01-01'))
        add_job(source,'source')
        with source.db() as db:
            for index in range(files):
                remote=f'disk:/files/{index:06}.bin';local=f'files/{index:06}.bin';sha=hashlib.sha256(str(index).encode()).hexdigest()
                meta=json.dumps({'path':remote,'type':'file','size':1,'sha256':sha})
                db.execute('INSERT INTO entries(job,path,kind,meta,local) VALUES(?,?,?,?,?)',('source',remote,'file',meta,local))
                db.execute('INSERT INTO names VALUES(?,?,?,?)',('source',remote,local,local.casefold()))
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',('source',remote,'original',local,1,sha,'fixture',None))
                db.execute('INSERT INTO verification_cache VALUES(?,?,?)',('source',local,'[]'))
            for index in range(service_records):
                local=f'journal-{index:06}.html'
                db.execute('INSERT INTO names VALUES(?,?,?,?)',('source','@output:'+local,local,local))
        previous={'store':source,'id':'source','job':source.job('source')}
        for label,module in (('before',baseline),('after',import_state)):
            samples=[]
            for index in range(runs):
                target=Store(folder/f'{label}-{index}.sqlite3');add_job(target,'target')
                original=module.safe_join;checks=[0];path_checks=[0]
                def tracked(root,local):
                    path_checks[0]+=1
                    if str(local).startswith('journal-'):checks[0]+=1
                    return original(root,local)
                module.safe_join=tracked
                try:
                    started=time.perf_counter();cpu=time.process_time()
                    module.inherit(target,'target',previous)
                    elapsed=time.perf_counter()-started;used_cpu=time.process_time()-cpu
                finally:module.safe_join=original
                with target.db() as db:
                    assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]==files
                    assert db.execute('SELECT COUNT(*) FROM baseline').fetchone()[0]==files
                    assert db.execute('SELECT COUNT(*) FROM verification_cache').fetchone()[0]==files
                    assert db.execute('SELECT COUNT(*) FROM names').fetchone()[0]==files+service_records
                samples.append({'seconds':round(elapsed,4),'cpu_seconds':round(used_cpu,4),'service_path_checks':checks[0],'filesystem_path_checks':path_checks[0]})
            results[label]={'samples':samples,'median_seconds':round(statistics.median(x['seconds'] for x in samples),4),'median_cpu_seconds':round(statistics.median(x['cpu_seconds'] for x in samples),4)}
    return {'files':files,'service_records':service_records,'runs':runs,'metadata_only':True,'results':results}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--baseline-ref',required=True);parser.add_argument('--files',type=int,default=2000);parser.add_argument('--service-records',type=int,default=6000);parser.add_argument('--runs',type=int,default=3)
    args=parser.parse_args()
    if not 1<=args.files<=100000 or not 0<=args.service_records<=100000 or not 1<=args.runs<=10:parser.error('Fixture size or repetition count is out of bounds')
    print(json.dumps(benchmark(args.baseline_ref,args.files,args.service_records,args.runs),indent=2))
