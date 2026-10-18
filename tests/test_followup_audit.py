import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from conftest import run_job,wait_job,test_timeout
from disk_downloader.api import Deadline,DeadlineAdapter,NetworkError
from disk_downloader.engine import Control


def test_blocked_dns_obeys_request_deadline(monkeypatch):
    import disk_downloader.api as module
    if hasattr(module,'DNS_RESOLVER'):
        from disk_downloader.dns import Resolver
        monkeypatch.setattr(module,'DNS_RESOLVER',Resolver())
    release=threading.Event();entered=threading.Event()
    def blocked(*args,**kwargs):
        entered.set();release.wait(test_timeout(2));raise socket.gaierror('simulated DNS stall')
    monkeypatch.setattr(socket,'getaddrinfo',blocked)
    started=time.monotonic()
    try:
        with pytest.raises((NetworkError,requests.RequestException)):
            with Deadline(.1) as deadline,requests.Session() as session:
                session.trust_env=False;session.mount('http://',DeadlineAdapter(deadline))
                session.get('http://audit.invalid/',timeout=(5,5))
        assert entered.is_set()
        assert time.monotonic()-started < test_timeout(.6)
    finally:release.set()


def test_deadline_start_failure_does_not_leak_registered_request(monkeypatch):
    control=Control();deadline=Deadline(2,control.check)
    def fail(self):raise RuntimeError('no thread resources')
    monkeypatch.setattr(threading.Thread,'start',fail)
    with pytest.raises(RuntimeError):deadline.__enter__()
    assert not control.network.requests


@pytest.mark.parametrize('action',['pack','save_manifest','refresh_journal'])
def test_paused_local_exports_resume_without_yandex_key(system,monkeypatch,action):
    engine,api,_=system
    api.file('disk:/one.bin',b'kept',preview=False)
    job=run_job(system,mode='fallback')
    api.credentials.clear()
    entered=threading.Event();release=threading.Event();phase=engine.phase
    def held(job_id,control,name):
        phase(job_id,control,name)
        if name=='preparing' and not entered.is_set():
            entered.set()
            while not release.is_set():control.check();time.sleep(.01)
    monkeypatch.setattr(engine,'phase',held)
    engine.action(job['id'],action)
    assert entered.wait(test_timeout(5))
    engine.action(job['id'],'pause')
    release.set()
    engine.action(job['id'],'resume')
    assert wait_job(engine,job['id'])['phase']=='completed'
    assert (Path(job['root'])/'one.bin').read_bytes()==b'kept'


def test_failed_temp_registration_removes_only_new_owned_file(system,monkeypatch):
    from disk_downloader.paths import temporary_file
    engine,_,tmp=system
    root=tmp/'results';root.mkdir();kept=root/'kept.bin';kept.write_bytes(b'keep')
    @contextmanager
    def broken():raise sqlite3.OperationalError('disk full');yield
    monkeypatch.setattr(engine.store,'db',broken)
    with pytest.raises(sqlite3.OperationalError):temporary_file(root,engine.store)
    assert [p.name for p in root.iterdir()]==['kept.bin']


def test_completed_outputs_leave_no_stale_temp_records(system):
    from disk_downloader.diagnostics import write_bundle
    engine,api,_=system
    api.file('disk:/one.bin',b'kept',preview=False)
    job=run_job(system,mode='fallback');write_bundle(engine,job['id'])
    rows=list(engine.store.rows('SELECT local FROM temporary_files'))
    assert rows==[]


def test_zip_is_fsynced_before_publication(system,monkeypatch):
    import disk_downloader.engine as module
    engine,api,_=system
    api.file('disk:/one.bin',b'kept',preview=False)
    synced=set();fsync=os.fsync;publish=module.publish
    def signature(info):return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    def observed(fd):
        fsync(fd);synced.add(signature(os.fstat(fd)))
    def publishing(source,target):
        if str(target).endswith('.zip'):assert signature(os.stat(source)) in synced
        return publish(source,target)
    monkeypatch.setattr(os,'fsync',observed);monkeypatch.setattr(module,'publish',publishing)
    assert run_job(system,mode='fallback')['phase']=='completed'



def test_failed_repack_preserves_previous_zip_reference(system,monkeypatch):
    import zipfile
    import disk_downloader.engine as module
    engine,api,_=system;api.file('disk:/one.bin',b'kept',preview=False)
    job=run_job(system,mode='fallback');previous=job['zip_path'];publish=module.publish
    def failing(source,target):
        if str(target).endswith('.zip'):raise OSError('simulated archive publication failure')
        return publish(source,target)
    monkeypatch.setattr(module,'publish',failing)
    engine.action(job['id'],'pack');failed=wait_job(engine,job['id'])
    assert failed['phase']=='failed' and failed['zip_path']==previous
    with zipfile.ZipFile(Path(job['root'])/previous) as archive:assert archive.testzip() is None


@pytest.mark.parametrize('failure',['setup','progress'])
def test_zip_cleans_owned_temp_even_when_database_write_fails(system,monkeypatch,failure):
    engine,api,_=system;api.file('disk:/one.bin',b'kept',preview=False)
    job=run_job(system,mode='fallback');original_db=engine.store.db
    class FaultyConnection:
        def __init__(self,db):self.db=db
        def __getattr__(self,name):return getattr(self.db,name)
        def execute(self,sql,*args):
            if (failure=='setup' and sql.startswith('UPDATE jobs SET zip_total=')) or (failure=='progress' and sql.startswith('UPDATE jobs SET zip_done=zip_done+')):
                raise sqlite3.OperationalError('simulated progress write failure')
            return self.db.execute(sql,*args)
    @contextmanager
    def faulty_db():
        with original_db() as db:yield FaultyConnection(db)
    monkeypatch.setattr(engine.store,'db',faulty_db)
    control=Control()
    with pytest.raises(sqlite3.OperationalError):engine.pack(job['id'],control,manual=True)
    assert not control.temporary and not control.temporary_sizes
    assert not list(Path(job['root']).glob('.disk-*.part'))
    assert not list(engine.store.rows('SELECT local FROM temporary_files'))
    assert (Path(job['root'])/'one.bin').read_bytes()==b'kept'


def test_dns_work_stays_bounded_after_many_cancelled_waits(monkeypatch):
    from disk_downloader.dns import Resolver
    resolver=Resolver(maximum=2);release=threading.Event();lock=threading.Lock();calls=[];errors=[]
    def blocked(host,*args):
        with lock:calls.append(host)
        release.wait(test_timeout(5))
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('127.0.0.1',80))]
    monkeypatch.setattr(socket,'getaddrinfo',blocked)
    def request(index):
        try:
            with Deadline(.12) as deadline:resolver.resolve(f'host-{index}.invalid',80,socket.AF_INET,deadline.check)
        except NetworkError:errors.append(index)
    clients=[threading.Thread(target=request,args=(i,)) for i in range(12)]
    try:
        for worker in clients:worker.start()
        for worker in clients:worker.join(test_timeout(2))
        assert all(not worker.is_alive() for worker in clients)
        assert len(errors)==12 and len(calls)==2 and len(resolver.pending)==2
    finally:
        release.set()
        for worker in clients:worker.join(test_timeout(2))


def test_dns_same_host_is_shared_and_cached(monkeypatch):
    from disk_downloader.dns import Resolver
    resolver=Resolver();release=threading.Event();entered=threading.Event();calls=[];results=[]
    def lookup(*args):
        calls.append(args);entered.set();release.wait(test_timeout(5))
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('127.0.0.1',80))]
    monkeypatch.setattr(socket,'getaddrinfo',lookup)
    def request():results.append(resolver.resolve('shared.invalid',80,socket.AF_INET,lambda:None))
    clients=[threading.Thread(target=request) for _ in range(8)]
    try:
        for worker in clients:worker.start()
        assert entered.wait(test_timeout(2));release.set()
        for worker in clients:worker.join(test_timeout(2))
        assert len(results)==8 and len(calls)==1
        assert resolver.resolve('shared.invalid',80,socket.AF_INET,lambda:None)==results[0]
        assert len(calls)==1
    finally:
        release.set()
        for worker in clients:worker.join(test_timeout(2))


def test_dns_cache_is_bounded_expires_and_invalidates(monkeypatch):
    from disk_downloader.dns import Resolver
    clock=[0];calls=[];resolver=Resolver(cache_size=2,ttl=1,clock=lambda:clock[0])
    def lookup(*args):
        calls.append(args[0]);return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
    monkeypatch.setattr(socket,'getaddrinfo',lookup)
    resolve=lambda host:resolver.resolve(host,80,socket.AF_INET,lambda:None)
    resolve('a');resolve('b');resolve('a');resolve('c');resolve('b')
    assert len(resolver.cache)==2 and calls==['a','b','c','b']
    clock[0]=2;resolve('b');assert len(calls)==5
    resolver.invalidate('b',80,socket.AF_INET);resolve('b');assert len(calls)==6


def test_dns_thread_start_failure_releases_slot(monkeypatch):
    from disk_downloader.dns import Resolver
    resolver=Resolver()
    def fail(self):raise RuntimeError('no worker resources')
    monkeypatch.setattr(threading.Thread,'start',fail)
    with pytest.raises(RuntimeError):resolver.resolve('host.invalid',80,socket.AF_INET,lambda:None)
    assert not resolver.pending


def test_stop_during_system_dns_returns_without_waiting_for_resolver(monkeypatch):
    import disk_downloader.api as module
    from disk_downloader.api import Suspended
    from disk_downloader.dns import Resolver
    monkeypatch.setattr(module,'DNS_RESOLVER',Resolver())
    entered=threading.Event();release=threading.Event();control=Control()
    def blocked(*args):
        entered.set();release.wait(test_timeout(5));raise socket.gaierror('simulated DNS failure')
    monkeypatch.setattr(socket,'getaddrinfo',blocked)
    timer=threading.Timer(.1,control.stop);timer.start();started=time.monotonic()
    try:
        with pytest.raises(Suspended):
            with Deadline(10,control.check,control.active_clock) as deadline,requests.Session() as session:
                session.trust_env=False;session.mount('http://',DeadlineAdapter(deadline))
                session.get('http://stopped.invalid/',timeout=10)
        assert entered.is_set() and time.monotonic()-started<test_timeout(.7)
    finally:release.set();timer.cancel()


def test_tls_handshake_stall_respects_remaining_budget():
    release=threading.Event();listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
    listener.settimeout(test_timeout(2));port=listener.getsockname()[1]
    def stall():
        try:
            client,_=listener.accept()
            with client:release.wait(test_timeout(3))
        except OSError:pass
    server=threading.Thread(target=stall,daemon=True);server.start();started=time.monotonic()
    try:
        with pytest.raises((NetworkError,requests.RequestException)):
            with Deadline(.15) as deadline,requests.Session() as session:
                session.trust_env=False;session.mount('https://',DeadlineAdapter(deadline))
                session.get(f'https://127.0.0.1:{port}/',timeout=(5,5))
        assert time.monotonic()-started<test_timeout(.8)
    finally:release.set();listener.close();server.join(test_timeout(3))


@pytest.fixture
def local_certificate(tmp_path):
    import shutil,subprocess
    executable=shutil.which('openssl')
    git=shutil.which('git')
    if not executable and git:
        for directory in ('usr/bin','mingw64/bin'):
            candidate=Path(git).resolve().parents[1]/directory/'openssl.exe'
            if candidate.is_file():executable=str(candidate);break
    if not executable:pytest.skip('OpenSSL is needed only to generate an ephemeral localhost test certificate')
    key,cert=tmp_path/'test-key.pem',tmp_path/'test-cert.pem'
    command=[executable,'req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','2','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost','-keyout',str(key),'-out',str(cert)]
    subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=test_timeout(15))
    return key,cert


def test_custom_connector_keeps_tls_hostname_verification_and_sni(monkeypatch,local_certificate):
    import ssl
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from disk_downloader.dns import Resolver
    import disk_downloader.api as module
    key,cert=local_certificate
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);seen=[]
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
    context.set_servername_callback(lambda sock,name,ctx:seen.append(name))
    server.socket=context.wrap_socket(server.socket,server_side=True)
    worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.05),daemon=True);worker.start()
    def lookup(host,port,*args):return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('127.0.0.1',port))]
    monkeypatch.setattr(socket,'getaddrinfo',lookup);monkeypatch.setattr(module,'DNS_RESOLVER',Resolver())
    try:
        with Deadline(3) as deadline,requests.Session() as session:
            session.trust_env=False;session.mount('https://',DeadlineAdapter(deadline))
            response=session.get(f'https://localhost:{server.server_port}/',verify=str(cert),timeout=(2,2))
            assert response.content==b'ok' and seen[-1]=='localhost'
        with Deadline(3) as deadline,requests.Session() as session:
            session.trust_env=False;session.mount('https://',DeadlineAdapter(deadline))
            with pytest.raises(requests.exceptions.SSLError):
                session.get(f'https://wrong-name.invalid:{server.server_port}/',verify=str(cert),timeout=(2,2))
        assert 'wrong-name.invalid' in seen
        with Deadline(3) as deadline,requests.Session() as session:
            session.trust_env=False;session.mount('https://',DeadlineAdapter(deadline))
            with pytest.raises(requests.exceptions.SSLError):
                session.get(f'https://localhost:{server.server_port}/',timeout=(2,2))
    finally:server.shutdown();server.server_close();worker.join(test_timeout(3))



def test_pause_during_dns_resumes_same_attempt(monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from disk_downloader.api import Credentials,DiskAPI
    from disk_downloader.dns import Resolver
    import disk_downloader.api as module
    entered=threading.Event();release=threading.Event();requests_seen=[];lookups=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.path);body=b'{"path":"disk:/","type":"dir","_embedded":{"items":[],"total":0}}'
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server_worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.05),daemon=True);server_worker.start()
    def lookup(host,port,*args):
        lookups.append(host);entered.set();release.wait(test_timeout(3))
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',port))]
    monkeypatch.setattr(socket,'getaddrinfo',lookup);monkeypatch.setattr(module,'DNS_RESOLVER',Resolver())
    monkeypatch.setattr(module,'BASE',f'http://dns-pause.invalid:{server.server_port}')
    credentials=Credentials();credentials.set('fake-token-for-tests');api=DiskAPI(credentials);control=Control();results=[];errors=[]
    def request():
        try:results.append(api.metadata('disk:/',{'attempts':1,'timeout':1,'total_timeout':.3},control.check))
        except Exception as exc:errors.append(exc)
    worker=threading.Thread(target=request);worker.start()
    try:
        assert entered.wait(test_timeout(2));control.pause();release.set();time.sleep(.12)
        assert worker.is_alive() and not errors
        control.resume();worker.join(test_timeout(3))
        assert not worker.is_alive() and not errors and results[0]['path']=='disk:/'
        assert len(requests_seen)==1 and len(lookups)==1 and not control.network.requests
    finally:
        release.set();control.stop();worker.join(test_timeout(3));server.shutdown();server.server_close();server_worker.join(test_timeout(3))
