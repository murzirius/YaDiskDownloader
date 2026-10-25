"""Isolated reproducible benchmark. Does not access Yandex or user state."""
import argparse
from collections import Counter, defaultdict, deque
import copy
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time
import zipfile

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--case',choices=['idle','history','summary','scan','download','pause','offline','missing','zip'],required=True)
parser.add_argument('--files',type=int,default=600)
parser.add_argument('--seconds',type=float,default=16)
parser.add_argument('--economy',action='store_true')
parser.add_argument('--early',action='store_true')
parser.add_argument('--profile',action='store_true',help='Collect SQL elapsed timings; do not use instrumented runs for speed comparisons')
parser.add_argument('--test-client',action='store_true',help='Use Flask test client when localhost TCP is blocked; excludes the HTTP transport')
args=parser.parse_args(); args.source=args.source.resolve();args.output=args.output.resolve()
if os.name!='nt':parser.error('This CPU/RAM/IO benchmark requires Windows; ordinary tests run on Linux too.')
assert not args.output.exists(), 'Use a new output directory'
args.output.mkdir(parents=True)
sys.path[:0]=[str(args.source),str(args.source/'tests')]
from conftest import FakeAPI, Response
from disk_downloader.api import NetworkError
from disk_downloader.web import create_app
from disk_downloader.tray import Tray
from disk_downloader.updates import Updates
from disk_downloader.paths import parent_path
from disk_downloader.network import NetworkWait
from waitress import create_server
import requests

Updates.start=lambda self,force=False:None
payload=bytes(range(256))*256
entered=threading.Event();online=threading.Event();online.set()
class SyntheticAPI(FakeAPI):
    def __init__(self):
        super().__init__(); self.pages=defaultdict(list);self.media_calls=deque(maxlen=100);self.metadata_calls=deque(maxlen=100)
        self.request_count=Counter();self.slow=False;self.outage=False;self.outages=0;self.media_successes=0;self.outage_started=None
    def index(self):
        for path,meta in self.tree.items():
            if path!='disk:/':self.pages[parent_path(path)].append(meta)
        for children in self.pages.values():children.sort(key=lambda m:m['name'])
    def metadata(self,path,settings=None,checkpoint=lambda:None,offset=0,limit=200,**kw):
        checkpoint(); self.request_count['metadata']+=1
        if self.outage and not online.is_set():raise NetworkError()
        meta=copy.deepcopy(self.tree[path])
        if meta['type']=='dir':meta['_embedded']={'items':copy.deepcopy(self.pages[path][offset:offset+limit]),'total':len(self.pages[path])}
        return meta
    def media(self,path,kind,settings,checkpoint=lambda:None):
        checkpoint();entered.set()
        with self.lock:
            self.request_count['media']+=1
            if self.outage and not self.outages:
                self.outages+=1;self.outage_started=time.monotonic();online.clear()
            if self.outage and not online.is_set():raise NetworkError()
        response,session=super().media(path,kind,settings,checkpoint)
        with self.lock:self.media_successes+=1
        response.chunks=65536
        if self.slow:
            original=response.iter_content
            def chunks(chunk_size):
                for chunk in original(chunk_size):
                    for _ in range(5):checkpoint();time.sleep(.02)
                    yield chunk
            response.iter_content=chunks
        return response,session
NetworkWait.reachable=staticmethod(online.is_set)
api=SyntheticAPI()
for index in range(args.files):api.file(f'disk:/group-{index//200:04}/{index:06}.bin',data=payload,preview=False,media_type='document',mime_type='application/octet-stream')
api.index()
with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
app=create_app(args.output/'state',api=api,port=port)
engine=app.extensions['engine'];store=engine.store;manager=app.extensions['management']
job=None
settings={'economy_mode':args.economy,'early_download':args.early,'destination':str(args.output/'results'),'whole_disk':True,'mode':'fallback','workers':8,'attempts':1,'timeout':20,'total_timeout':0,'auto_pause':False,'create_zip':False,'reports':['json']}
def wait_job(key,timeout=240):
    worker=engine.threads.get(key)
    if worker:worker.join(timeout)
    assert not worker or not worker.is_alive(), 'Worker timed out'
    result=store.job(key)
    assert result['phase'] not in ('failed','completed_with_errors'),result['error']
    return result
if args.case in ('history','summary'):
    root=args.output/'results';root.mkdir()
    normalized=engine.validate_settings(settings)
    job='large-history'
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES(?,?,?,?,?,?,1)',(job,json.dumps(normalized),str(root),str(root),'completed','2026-01-01'))
        db.executemany('INSERT INTO entries(job,path,kind,meta,local,status) VALUES(?,?,?,?,?,?)',((job,path,'file',json.dumps(meta),path[6:],'pending') for path,meta in api.tree.items() if meta['type']=='file'))
elif args.case not in ('idle','scan'):
    job=engine.create(settings);wait_job(job)
    if args.case in ('missing','zip'):
        engine.action(job,'start');wait_job(job)

# Trace only the measured interval; no SQL contents or user identifiers saved.
sql=Counter();connections=0;count_lock=threading.Lock();measuring=False
original_connect=sqlite3.connect
def connect(*a,**kw):
    global connections
    conn=original_connect(*a,**kw)
    if measuring:
        with count_lock:connections+=1
    def trace(statement):
        verb=statement.split(None,1)[0].upper() if statement.strip() else ''
        if measuring:
            with count_lock:sql[verb]+=1
    conn.set_trace_callback(trace)
    return conn
sqlite3.connect=connect
sql_timings = defaultdict(lambda: [0,0.0])
if args.profile:
    from disk_downloader.sqlite_writes import WriteConnection
    def instrument(name):
        original=getattr(WriteConnection,name)
        def wrapped(connection,*a,**kw):
            started=time.perf_counter()
            try:return original(connection,*a,**kw)
            finally:
                if measuring:
                    label=name
                    if name in ('execute','executemany') and a:
                        label=re.sub(r"'(?:''|[^'])*'",'?',a[0])
                        label=re.sub(r'\b[0-9]+\b','?',label)
                        label=' '.join(label.split())[:180]
                    with count_lock:
                        sql_timings[label][0]+=1
                        sql_timings[label][1]+=time.perf_counter()-started
        setattr(WriteConnection,name,wrapped)
    for name in ('execute','executemany','commit','close'):instrument(name)

class Memory(c.Structure):
    _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(n,c.c_size_t) for n in ('peak','rss','pp','p','np','n','page','peakpage','private')]
class IO(c.Structure):
    _fields_=[(n,c.c_ulonglong) for n in ('reads','writes','other','read_bytes','write_bytes','other_bytes')]
kernel=c.WinDLL('kernel32');kernel.GetCurrentProcess.restype=w.HANDLE
handle=kernel.GetCurrentProcess()
getmemory=c.WinDLL('psapi').GetProcessMemoryInfo;getmemory.argtypes=[w.HANDLE,c.POINTER(Memory),w.DWORD]
kernel.GetProcessIoCounters.argtypes=[w.HANDLE,c.POINTER(IO)]
def memory():
    value=Memory();value.cb=c.sizeof(value);assert getmemory(handle,c.byref(value),value.cb);return value.rss
def counters():
    value=IO();assert kernel.GetProcessIoCounters(handle,c.byref(value));return {n:getattr(value,n) for n,_ in IO._fields_}
stop=threading.Event();samples=[];latencies=[];requests_count=0;poll_errors=[]
def sample():
    while not stop.wait(.05):samples.append(memory())
server=None if args.test_client else create_server(app,host='127.0.0.1',port=port,threads=12)
def serve():
    try:server.run()
    except OSError:
        if not stop.is_set():raise
if server:
    server_thread=threading.Thread(target=serve,daemon=True);server_thread.start()
base=f'http://127.0.0.1:{port}'
class TestSession:
    def __init__(self):self.client=app.test_client();self.cookies={}
    def get(self,url,headers=None,timeout=None):
        response=self.client.get(url.removeprefix(base),base_url=base,headers=headers)
        class Result:
            text=response.get_data(as_text=True)
            def raise_for_status(self):assert response.status_code==200,response.get_data(as_text=True)
        return Result()
    def close(self):pass
def client_session():
    if args.test_client:return TestSession()
    client=requests.Session();client.trust_env=False;return client
session=client_session()
page=session.get(base+'/',timeout=5)
headers={'X-CSRF-Token':re.search(r'name="csrf-token" content="([^"]+)"',page.text).group(1),'Origin':base}
def poll():
    global requests_count
    client=session if args.test_client else client_session()
    if not args.test_client:client.cookies.update(session.cookies)
    index=0
    try:
        while not stop.is_set():
            paths=['/api/jobs/'+job] if job else ['/api/status']
            if index%3==0 and job:paths.append('/api/status')
            for path in paths:
                before=time.perf_counter();response=client.get(base+path,headers=headers,timeout=15)
                response.raise_for_status();latencies.append(time.perf_counter()-before);requests_count+=1
            index+=1;stop.wait(1)
    except BaseException as exc:poll_errors.append(repr(exc))
    finally:client.close()
manager.start()
tray=Tray(engine,base,lambda:None)
notification=threading.Thread(target=tray.watch_notifications,daemon=True);notification.start()
if args.case=='pause':
    api.slow=True;engine.action(job,'start');assert entered.wait(10);engine.action(job,'pause');time.sleep(.3)
if args.case=='offline':api.slow=True;api.outage=True
before_io=counters();before_cpu=time.process_time();before_wall=time.perf_counter();before_rss=memory();measuring=True
sampler=threading.Thread(target=sample,daemon=True);sampler.start()
poller=None
if args.case!='summary':poller=threading.Thread(target=poll,daemon=True);poller.start()
correct={};offline_validation=None
try:
    if args.case in ('idle','history','pause'):
        stop.wait(args.seconds)
    elif args.case=='summary':
        for _ in range(8):value=store.summary(job)
        correct={'files':value['files'],'processed':value['processed']};assert value['files']==args.files
    elif args.case=='scan':
        job=engine.create(settings);value=wait_job(job);assert value['phase']=='ready'
        with store.db() as db:assert db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND kind='file'",(job,)).fetchone()[0]==args.files
        correct={'files':args.files}
    elif args.case in ('download','offline'):
        engine.action(job,'start')
        if args.case=='offline':
            network=engine.controls[job].network
            try:
                assert network.offline.wait(10), 'No real NetworkWait transition'
                assert engine.status(job)['network_waiting']
                time.sleep(5)
                with api.lock:successes=api.media_successes
                with store.db() as db:assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job,)).fetchone()[0]==0
                assert not online.is_set() and network.offline.is_set() and successes==0
                offline_validation={'waiting_observed':True,'successes_before_recovery':successes,'outage_seconds':time.monotonic()-api.outage_started}
            finally:online.set()
        value=wait_job(job);assert value['phase']=='completed'
        if args.case=='offline':
            offline_validation['network_wait_seconds']=network.elapsed
            assert network.elapsed>=5 and not network.offline.is_set()
    elif args.case=='missing':
        job=engine.create(settings|{'operation':'missing','destination':store.job(job)['root'],'missing_policy':'any_result'})
        wait_job(job);engine.action(job,'start');value=wait_job(job);assert value['phase']=='completed'
    elif args.case=='zip':engine.action(job,'pack');value=wait_job(job);assert value['zip_path']
finally:
    elapsed=time.perf_counter()-before_wall;cpu=time.process_time()-before_cpu;after_io=counters();measuring=False
    stop.set();sampler.join();
    if poller:poller.join(20)
    manager.stopped.set();manager.thread.join(2);tray.notification_stop.set();notification.join(2)

assert not poll_errors,poll_errors

if args.case=='pause':engine.action(job,'stop');wait_job(job)
if args.case in ('download','missing','zip','offline'):
    expected=hashlib.sha256(payload).hexdigest();verified=0
    for row in store.rows('SELECT local,sha256 FROM artifacts WHERE job=?',(job,)):
        data=(Path(value['root'])/row['local']).read_bytes();assert hashlib.sha256(data).hexdigest()==expected==row['sha256'];verified+=1
    assert verified==args.files
    correct={'verified_files':verified,'sha256_ok':True}
    if args.case=='zip':
        with zipfile.ZipFile(Path(value['root'])/value['zip_path']) as archive:
            assert archive.testzip() is None;assert sum(n.endswith('.bin') for n in archive.namelist())==args.files
        correct['zip_ok']=True
with store.db() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
correct['sqlite_ok']=True
if args.profile:
    timings=sorted(({'operation':name,'calls':value[0],'sum_elapsed_seconds':value[1]} for name,value in sql_timings.items()),key=lambda item:item['sum_elapsed_seconds'],reverse=True)
    (args.output/'sql-profile.json').write_text(json.dumps(timings,indent=2),encoding='utf-8')

ordered=sorted(latencies)
result={'case':args.case,'files':args.files,'seconds':elapsed,'cpu_seconds':cpu,'cpu_one_core_percent':100*cpu/elapsed,
        'rss_start':before_rss,'rss_mean':sum(samples)/len(samples) if samples else memory(),'rss_peak':max(samples,default=memory()),
        'io':{k:after_io[k]-before_io[k] for k in after_io},'sqlite_connections':connections,'sql':dict(sql),'http_requests':requests_count,
        'http_p95_ms':1000*ordered[min(len(ordered)-1,int(len(ordered)*.95))] if ordered else None,
        'network_requests':dict(api.request_count),'outages':api.outages,'correctness':correct,'sqlite':sqlite3.sqlite_version,'python':sys.version.split()[0],
        'profiled':args.profile,'http_transport':'flask-test-client' if args.test_client else 'tcp-waitress','source':str(args.source)}
if offline_validation is not None:result['offline_validation']=offline_validation
(args.output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result),flush=True)
session.close()
if server:
    server.close()
    for channel in list(server._map.values()):channel.close()
    server.task_dispatcher.shutdown(timeout=1)
