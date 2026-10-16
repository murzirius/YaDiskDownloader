"""Real-time, isolated durability/load run. Requires requirements-dev.txt.

No Yandex account, network requests or user data. All files live under --output.
--minutes is real elapsed time, not an accelerated clock. A run always finishes
at least one complete inventory/download/verification cycle before succeeding.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'tests')]


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp,path)


def rss_bytes():
    if os.name != 'nt':
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    class Counters(ctypes.Structure):
        _fields_ = [('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong)] + [(name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    memory_info = ctypes.WinDLL('psapi').GetProcessMemoryInfo
    memory_info.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    if not memory_info(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        raise ctypes.WinError()
    return counters.WorkingSetSize


def worker(args):
    from conftest import FakeAPI, Response
    from disk_downloader.engine import Engine
    from disk_downloader.store import Store
    from disk_downloader.api import NetworkError
    from disk_downloader.network import NetworkWait
    from disk_downloader.recovery import recover_publications
    directory = args.output
    payload = bytes(range(256))*4
    api = FakeAPI()
    for index in range(args.files):
        api.file(f'disk:/group-{index//200:04}/{index:06}.bin',data=payload,preview=False)
    # Bound call tracing so a day-long test measures the service, not a growing
    # fake-API debug list.
    from collections import deque
    api.media_calls = deque(maxlen=100)
    api.metadata_calls = deque(maxlen=100)
    offline_until = [0.0]
    NetworkWait.reachable = staticmethod(lambda:time.monotonic() >= offline_until[0])
    store = Store(directory/'state/jobs.sqlite3')
    recover_publications(store)
    engine = Engine(store,api)
    original_media = api.media
    requested = set()
    outages = [0]
    def media(path, kind, settings, checkpoint=lambda:None):
        index = int(path.rsplit('/',1)[-1].split('.')[0])
        if index % 997 == 500 and path not in requested:
            requested.add(path)
            outages[0] += 1
            offline_until[0] = time.monotonic()+.4
            control = next(iter(engine.controls.values()))
            control.network.inspect(force=True)
            raise NetworkError(offline=True)
        response,session = original_media(path,kind,settings,checkpoint)
        response.chunks = 256
        return response,session
    api.media = media
    if args.slow_fsync_ms:
        original_fsync = os.fsync
        def slow_fsync(fd):
            time.sleep(args.slow_fsync_ms/1000)
            return original_fsync(fd)
        os.fsync = slow_fsync
    existing = list(store.rows('SELECT id FROM jobs ORDER BY started DESC LIMIT 1'))
    if existing:
        job = existing[0]['id']
        engine.action(job,'resume')
    else:
        job = engine.create({'whole_disk':True,'destination':str(directory/'results'),
                             'mode':'fallback','workers':8,'attempts':1,'timeout':20,
                             'create_zip':False,'auto_pause':False,'reports':['json']})
    started = time.monotonic()
    peak = 0
    while True:
        thread = engine.threads.get(job)
        if thread:
            thread.join(.5)
        status = store.summary(job)
        peak = max(peak,rss_bytes())
        atomic_json(directory/'progress.json',{'phase':status['phase'],'processed':status['processed'],
            'originals':status['originals'],'files':status['files'],'errors':status['errors'],
            'rss_peak_bytes':peak,'elapsed_seconds':time.monotonic()-started,'outages':outages[0]})
        if thread and thread.is_alive():
            continue
        if status['phase'] == 'ready':
            assert status['files'] == args.files
            engine.action(job,'start')
            continue
        assert status['phase'] == 'completed', status
        assert status['completeness'] == {'selected':args.files,'originals':args.files,'preview_only':0,'no_result':0}
        break
    expected = hashlib.sha256(payload).hexdigest()
    for row in store.rows('SELECT local,sha256 FROM artifacts WHERE job=?',(job,)):
        assert row['sha256'] == expected
        assert hashlib.sha256((Path(status['root'])/row['local']).read_bytes()).hexdigest() == expected
    with store.db() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('SELECT * FROM pending_publications').fetchall()
        assert not db.execute("SELECT * FROM entries WHERE job=? AND status='running'",(job,)).fetchall()
    atomic_json(directory/'worker-result.json',{'verified_files':args.files,'sha256_ok':True,
        'database_integrity':'ok','rss_peak_bytes':peak,'outages':outages[0],
        'seconds':time.monotonic()-started,'sqlite':sqlite3.sqlite_version})


def run(args):
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('--output must be a new or empty directory')
    args.output.mkdir(parents=True,exist_ok=True)
    started = time.monotonic()
    result = {'requested_minutes':args.minutes,'files_per_cycle':args.files,
              'slow_fsync_ms':args.slow_fsync_ms,'clock':'real monotonic time',
              'cycles':[],'hard_kills':0,'success':False}
    cycle = 0
    try:
        while cycle == 0 or time.monotonic()-started < args.minutes*60:
            directory = args.output/f'cycle-{cycle:04}'
            directory.mkdir()
            killed = False
            first_peak = 0
            first_outages = 0
            for restart in range(2):
                command = [sys.executable,str(Path(__file__).resolve()),'--worker','--output',str(directory),
                           '--files',str(args.files),'--slow-fsync-ms',str(args.slow_fsync_ms)]
                with (directory/f'worker-{restart}.log').open('w',encoding='utf-8') as log:
                    process = subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
                                               env=dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8'))
                    try:
                        while process.poll() is None:
                            elapsed = time.monotonic()-started
                            if elapsed > args.max_seconds:
                                raise TimeoutError('Soak maximum wall time exceeded')
                            progress = directory/'progress.json'
                            if restart == 0 and progress.exists():
                                snapshot = json.loads(progress.read_text(encoding='utf-8'))
                                first_peak = max(first_peak,snapshot['rss_peak_bytes'])
                                first_outages = max(first_outages,snapshot['outages'])
                                if snapshot['originals'] >= max(2,args.files//3) and snapshot['phase'] == 'downloading':
                                    process.kill()  # TerminateProcess/SIGKILL, no cleanup path.
                                    process.wait(timeout=10)
                                    killed = True
                                    result['hard_kills'] += 1
                                    break
                            time.sleep(.2)
                        if not killed or restart:
                            assert process.returncode == 0, (directory,process.returncode)
                            break
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=10)
            assert killed, 'Run completed too quickly to exercise abrupt process loss'
            item = json.loads((directory/'worker-result.json').read_text(encoding='utf-8'))
            item['rss_peak_bytes'] = max(first_peak,item['rss_peak_bytes'])
            item['outages'] += first_outages
            result['cycles'].append(item)
            cycle += 1
            print(json.dumps({'cycles':cycle,'elapsed_seconds':round(time.monotonic()-started),**item}),flush=True)
            atomic_json(args.output/'result.json', result | {'actual_seconds':time.monotonic()-started})
            if not args.keep_data:
                # Only our freshly created, verified worker datasets. Preserve
                # measurements/logs and preserve all data on a failed cycle.
                for name in ('results','state'):
                    target = directory/name
                    resolved = target.resolve(strict=True)
                    if not resolved.is_relative_to(directory.resolve()) or resolved != target.absolute() or target.is_symlink():
                        raise RuntimeError('Refusing cleanup outside the owned test cycle')
                    if getattr(target.stat(follow_symlinks=False),'st_file_attributes',0) & 0x400:
                        raise RuntimeError('Refusing cleanup of a reparse point')
                    shutil.rmtree(target)
        result['success'] = True
    finally:
        result['actual_seconds'] = time.monotonic()-started
        atomic_json(args.output/'result.json',result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--files',type=int,default=20000)
    parser.add_argument('--minutes',type=float,default=60)
    parser.add_argument('--max-seconds',type=float,default=21000)
    parser.add_argument('--slow-fsync-ms',type=float,default=.5)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--keep-data',action='store_true',help='Keep verified synthetic datasets; failed cycles are always kept')
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not 10 <= args.files <= 100000 or args.minutes < 0 or args.slow_fsync_ms < 0:
        parser.error('Invalid workload parameters')
    (worker if args.worker else run)(args)
