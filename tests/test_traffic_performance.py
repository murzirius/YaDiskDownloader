import gzip
import hashlib
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from conftest import FakeAPI, make_job, run_job, wait_job
from disk_downloader.api import Credentials, DiskAPI, METADATA_FIELDS
from disk_downloader.performance import MemoryCache, StatusDeltas
from disk_downloader.traffic import NetworkUsage
from disk_downloader.web import create_app
from disk_downloader.store import Store


def client_for(app):
    client = app.test_client()
    base = 'http://127.0.0.1:8766'
    client.get('/', base_url=base)
    with client.session_transaction(base_url=base) as session:
        csrf = session['csrf']
    return client, base, {'Origin': base, 'X-CSRF-Token': csrf}


def test_cache_bounds_ttl_and_account_scope():
    clock = [10]
    cache = MemoryCache(max_bytes=8, max_items=2, clock=lambda: clock[0])
    cache.select_scope(1)
    cache.put('a', b'1234', 4, ttl=1)
    cache.put('b', b'5678', 4)
    assert cache.get('a') == b'1234'
    cache.put('c', b'9999', 4)
    assert cache.get('b') is None
    clock[0] = 12
    assert cache.get('a') is None
    cache.select_scope(2)
    assert cache.get('c') is None
    assert cache.snapshot()['bytes'] == 0
    cache.put('huge', b'x' * 9, 9)
    assert cache.get('huge') is None


def test_thumbnail_cache_is_invalidated_by_listing_and_credentials(tmp_path):
    api = FakeAPI()
    api.file('disk:/photo.jpg')
    app = create_app(tmp_path / 'state', api)
    client, base, headers = client_for(app)
    get = lambda url: client.get(url, base_url=base, headers=headers)
    assert get('/api/browse').status_code == 200
    for _ in range(3):
        assert get('/api/thumbnail?path=disk:/photo.jpg&size=S').status_code == 200
    assert len(api.media_calls) == 1
    api.tree['disk:/photo.jpg']['modified'] = '2030-01-01T00:00:00Z'
    assert get('/api/browse').status_code == 200
    assert get('/api/thumbnail?path=disk:/photo.jpg&size=S').status_code == 200
    assert len(api.media_calls) == 2
    api.credentials.set('another-fake-token')
    assert get('/api/thumbnail?path=disk:/photo.jpg&size=S').status_code == 200
    assert len(api.media_calls) == 3
    client.post('/api/disconnect', json={}, base_url=base, headers=headers)
    assert app.extensions['memory_cache'].snapshot()['bytes'] == 0
    assert get('/api/thumbnail?path=disk:/photo.jpg&size=S').status_code != 200


def test_delta_fallback_and_account_boundary():
    cache = StatusDeltas()
    job = dict(id='j', instance_id='one', auth_revision=1, phase='ready', settings={'paths':['x']})
    first = cache.response(job, '')
    delta = cache.response(job | {'phase':'downloading'}, first['revision'])
    assert delta['delta'] and delta['values'] == {'phase':'downloading'}
    assert not cache.response(job, 'lost-revision')['delta']
    assert not cache.response(job | {'auth_revision':2}, delta['revision'])['delta']
    assert not cache.response(job | {'instance_id':'two'}, delta['revision'])['delta']


def test_traffic_reservation_survives_crash_without_chunk_writes(tmp_path):
    store = Store(tmp_path / 'jobs.sqlite3')
    usage = NetworkUsage(store)
    usage.configure(1)
    for _ in range(100):
        usage.received(1024, 'media')
    assert usage.snapshot()['bytes'] == 102400
    with store.db() as db:
        assert db.execute('SELECT bytes FROM traffic_usage').fetchone()[0] == 1024 * 1024
    recovered = NetworkUsage(store)
    assert recovered.exceeded()
    assert recovered.snapshot()['bytes'] >= usage.snapshot()['bytes']
    recovered.configure(0)
    assert not recovered.exceeded()


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), '10', 1000001])
def test_invalid_traffic_limit(value):
    with pytest.raises(ValueError):
        NetworkUsage.validate(value)


def test_economy_uses_recent_hash_metadata_for_originals(system):
    engine, api, tmp = system
    for i in range(12):
        api.file('disk:/item%d.bin' % i, b'payload' * 100, preview=False)
    job = engine.create(dict(destination=str(tmp / 'result'), paths=['disk:/'], mode='fallback', economy_mode=True, auto_start=True, create_zip=False))
    status = wait_job(engine, job)
    assert status['phase'] == 'completed', status
    assert len(api.metadata_calls) < 12
    with engine.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?', (job,)).fetchone()[0] == 12


def test_early_download_is_confirmed_before_later_folder_scan(system):
    engine, api, tmp = system
    api.file('disk:/a.bin', b'first', preview=False)
    api.directory('disk:/later')
    api.file('disk:/later/b.bin', b'second', preview=False)
    original = api.metadata
    seen = []
    def metadata(path, *args, **kwargs):
        if path == 'disk:/later':
            seen.append(bool(api.media_calls))
        return original(path, *args, **kwargs)
    api.metadata = metadata
    job = engine.create(dict(destination=str(tmp / 'result'), paths=['disk:/'], mode='fallback', early_download=True, auto_start=True, create_zip=False))
    status = wait_job(engine, job)
    assert status['phase'] == 'completed', status
    assert seen and all(seen)
    assert status['files'] == 2


def test_finish_current_does_not_claim_unstarted_files(system):
    engine, api, _ = system
    for i in range(8):
        api.file('disk:/item%d.bin' % i, b'content', preview=False)
    job = make_job(system, mode='fallback')
    entered, release = threading.Event(), threading.Event()
    original = engine.process
    def process(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    engine.process = process
    engine.action(job, 'start')
    assert entered.wait(10)
    engine.action(job, 'finish_current')
    release.set()
    status = wait_job(engine, job)
    assert status['phase'] == 'stopped'
    assert 0 < len(api.media_calls) <= 2
    with engine.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM entries WHERE job=? AND status='running'", (job,)).fetchone()[0] == 0
    engine.action(job, 'resume')
    assert wait_job(engine, job)['phase'] == 'completed'


def test_real_json_connections_reuse_gzip_and_epoch_isolation(monkeypatch):
    import disk_downloader.api as module
    opened = []
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def get_request(self):
            conn, addr = super().get_request()
            opened.append(conn)
            return conn, addr
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_GET(self):
            assert self.headers['Accept-Encoding'] == 'gzip'
            content = gzip.compress(json.dumps({'path':'disk:/', 'type':'dir', 'name':'Disk', '_embedded':{'items':[], 'total':0}}).encode())
            self.send_response(200)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    credentials = Credentials();credentials.set('synthetic-token')
    api = DiskAPI(credentials)
    monkeypatch.setattr(module, 'BASE', 'http://127.0.0.1:%d' % server.server_port)
    try:
        for _ in range(5):
            assert api.metadata('disk:/')['type'] == 'dir'
        assert len(opened) == 1
        credentials.set('changed-synthetic-token')
        api.metadata('disk:/')
        assert len(opened) == 2
    finally:
        api.json_connections.close_idle()
        server.shutdown();server.server_close();thread.join(2)


def test_fields_preserve_integrity_dates_paging(monkeypatch):
    credentials=Credentials();credentials.set('synthetic-token')
    api=DiskAPI(credentials)
    captured=[]
    def get(endpoint, params, settings, checkpoint):
        captured.append(params)
        return {'path':'disk:/','name':'Disk','type':'dir'}
    monkeypatch.setattr(api,'get',get)
    api.metadata('disk:/')
    fields=set(captured[0]['fields'].split(','))
    assert {'sha256','md5','size','modified','created','preview','exif.date_time','_embedded.total','_embedded.items.sha256'} <= fields


def test_retry_error_group_does_not_retry_unrelated_files(system):
    engine, api, _ = system
    api.file('disk:/one.bin', b'one', preview=False)
    api.file('disk:/two.bin', b'two', preview=False)
    job=make_job(system, mode='fallback')
    with engine.store.db() as db:
        db.execute("UPDATE entries SET status='error',error='group one' WHERE job=? AND path='disk:/one.bin'", (job,))
        db.execute("UPDATE entries SET status='error',error='group two' WHERE job=? AND path='disk:/two.bin'", (job,))
    engine.action(job, 'retry_group', {'problem_error':'group one'})
    wait_job(engine, job)
    with engine.store.db() as db:
        assert db.execute("SELECT error FROM entries WHERE job=? AND path='disk:/two.bin'",(job,)).fetchone()[0] == 'group two'
    assert len(api.media_calls) == 1


def test_warm_connection_timeout_is_closed_and_next_request_recovers(monkeypatch):
    import disk_downloader.api as module
    from disk_downloader.api import NetworkError
    requests_seen=[]
    opened=[]
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def get_request(self):
            conn,addr=super().get_request();opened.append(conn);return conn,addr
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            requests_seen.append(1)
            if len(requests_seen)==2:time.sleep(0.7)
            data=b'{"path":"disk:/","name":"Disk","type":"dir"}'
            try:
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except OSError:pass
    server=Server(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    credentials=Credentials();credentials.set('synthetic-token')
    api=DiskAPI(credentials)
    monkeypatch.setattr(module,'BASE','http://127.0.0.1:%d'%server.server_port)
    try:
        api.metadata('disk:/')
        start=time.monotonic()
        with pytest.raises(NetworkError):api.metadata('disk:/',{'attempts':1,'timeout':0.15})
        assert time.monotonic()-start<0.6
        api.metadata('disk:/')
        assert len(opened)==2
    finally:
        api.json_connections.close_idle();server.shutdown();server.server_close();worker.join(2)


def test_browse_cannot_publish_or_cache_previous_account_response(tmp_path):
    api=FakeAPI();api.file('disk:/photo.jpg')
    original=api.metadata
    def switched(*args,**kwargs):
        response=original(*args,**kwargs)
        api.credentials.set('replacement-synthetic-key')
        return response
    api.metadata=switched
    app=create_app(tmp_path/'state',api)
    client,base,headers=client_for(app)
    response=client.get('/api/browse',base_url=base,headers=headers)
    assert response.status_code!=200
    assert 'items' not in response.get_json()
    assert app.extensions['memory_cache'].snapshot()['bytes']==0


@pytest.mark.parametrize('args,endpoint', [(['traffic','--limit-mb','10','--json'],'/api/traffic'),(['finish','job','--json'],'/api/jobs/job/action')])
def test_linux_cli_traffic_and_finish(monkeypatch,args,endpoint):
    import disk_downloader.cli as cli
    calls=[]
    class Client:
        def __init__(self,*args):pass
        def call(self,path,data=None):calls.append((path,data));return {'ok':True}
    monkeypatch.setattr(cli.sys,'platform','linux');monkeypatch.setattr(cli,'Client',Client)
    assert cli.main(args)==0
    assert calls[0][0]==endpoint


def test_daily_quota_pauses_real_json_read_and_resumes_without_spending_attempt(tmp_path,monkeypatch):
    import disk_downloader.api as module
    from disk_downloader.engine import Control
    content=json.dumps({'path':'disk:/','type':'dir','name':'Disk','note':'x'*150000}).encode()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            try:
                self.send_response(200);self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    credentials=Credentials();credentials.set('synthetic-token');api=DiskAPI(credentials)
    usage=NetworkUsage(Store(tmp_path/'usage.sqlite3'));usage.configure(0.01);api.traffic=usage
    monkeypatch.setattr(module,'BASE','http://127.0.0.1:%d'%server.server_port)
    control=Control();result=[];errors=[]
    def request():
        try:result.append(api.metadata('disk:/',{'attempts':1,'timeout':5},control.check))
        except BaseException as error:errors.append(error)
    worker=threading.Thread(target=request);worker.start()
    try:
        until=time.monotonic()+5
        while not control.paused and worker.is_alive() and time.monotonic()<until:time.sleep(0.01)
        assert control.paused and worker.is_alive()
        assert 'дневной лимит' in control.pause_reason
        usage.configure(0);control.resume();worker.join(10)
        assert not worker.is_alive() and not errors
        assert result and result[0]['name']=='Disk'
        usage.flush()
        assert NetworkUsage(usage.store).snapshot()['bytes']==usage.snapshot()['bytes']
    finally:
        control.stop();worker.join(10);api.json_connections.close_idle()
        server.shutdown();server.server_close();thread.join(2)


@pytest.mark.skipif(__import__('os').name!='nt',reason='Git OpenSSL fixture on Windows')
def test_real_https_pool_keeps_certificate_verification(tmp_path,monkeypatch):
    import os
    import shutil
    import ssl
    import subprocess
    import disk_downloader.api as module
    import requests
    from disk_downloader.api import NetworkError
    git=shutil.which('git')
    openssl=Path(git).parent.parent/'usr/bin/openssl.exe' if git else Path('missing')
    if not openssl.is_file():pytest.skip('Git OpenSSL is unavailable')
    cert,key=tmp_path/'localhost.crt',tmp_path/'localhost.key'
    subprocess.run([str(openssl),'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,capture_output=True)
    connections=[]
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def get_request(self):
            conn,addr=super().get_request();connections.append(conn);return conn,addr
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            data=b'{"path":"disk:/","name":"Disk","type":"dir"}'
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=Server(('127.0.0.1',0),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    credentials=Credentials();credentials.set('synthetic-token');api=DiskAPI(credentials)
    monkeypatch.setattr(module,'BASE','https://localhost:%d'%server.server_port)
    try:
        with pytest.raises(NetworkError):api.metadata('disk:/',{'attempts':1,'timeout':3})
        get=requests.Session.get
        def trusted(session,*args,**kwargs):
            kwargs['verify']=str(cert)
            return get(session,*args,**kwargs)
        monkeypatch.setattr(requests.Session,'get',trusted)
        for _ in range(3):assert api.metadata('disk:/')['type']=='dir'
        assert len(connections)==1
    finally:
        api.json_connections.close_idle();server.shutdown();server.server_close();worker.join(2)


def test_failed_connection_is_included_in_bottleneck_measurements(tmp_path,monkeypatch):
    import requests
    credentials=Credentials();credentials.set('synthetic-token');api=DiskAPI(credentials)
    api.traffic=NetworkUsage(Store(tmp_path/'usage.sqlite3'))
    def failed(*args,**kwargs):raise requests.ConnectTimeout('Synthetic connection timeout')
    monkeypatch.setattr(requests.Session,'get',failed)
    from disk_downloader.api import NetworkError
    with pytest.raises(NetworkError):api.metadata('disk:/',{'attempts':1,'timeout':1})
    assert api.traffic.snapshot()['requests']==1


def test_background_scan_releases_idle_api_sockets(tmp_path,monkeypatch):
    import disk_downloader.api as module
    from disk_downloader.engine import Engine
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            data=b'{"path":"disk:/","name":"Disk","type":"dir","_embedded":{"items":[],"total":0}}'
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    credentials=Credentials();credentials.set('synthetic-token');api=DiskAPI(credentials)
    monkeypatch.setattr(module,'BASE','http://127.0.0.1:%d'%server.server_port)
    try:
        engine=Engine(Store(tmp_path/'state/jobs.sqlite3'),api)
        job=engine.create({'paths':['disk:/'],'destination':str(tmp_path/'downloads'),'create_zip':False})
        assert wait_job(engine,job)['phase']=='ready'
        assert len(api.json_connections.items)==0
    finally:
        credentials.clear();api.json_connections.close_idle();server.shutdown();server.server_close();worker.join(2)
