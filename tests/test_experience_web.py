import re
import threading
import time
from pathlib import Path

import pytest

from conftest import run_job, test_timeout
from test_api_security import web_session
from disk_downloader.web import create_app


def test_destination_probe_is_protected_and_does_not_create_destination(tmp_path):
    app,client,base,headers=web_session(tmp_path)
    destination=tmp_path/'not-created'/'photos'
    assert client.post('/api/destination',base_url=base,json={'path':str(destination)}).status_code==403
    response=client.post('/api/destination',base_url=base,headers=headers,json={'path':str(destination)})
    assert response.status_code==200 and response.json['writable'] and not response.json['exists']
    assert response.json['free_bytes']>0 and not destination.exists()
    assert not list(tmp_path.glob('.yadisk-write-test-*'))
    file=tmp_path/'ordinary.txt';file.write_text('keep',encoding='utf-8')
    assert client.post('/api/destination',base_url=base,headers=headers,json={'path':str(file)}).status_code==400
    assert file.read_text(encoding='utf-8')=='keep'


def test_repair_and_recovery_endpoints_require_csrf(tmp_path):
    app,client,base,headers=web_session(tmp_path)
    for path in ('/api/verification/repair','/api/recovery'):
        assert client.post(path,base_url=base,json={}).status_code==403
    response=client.post('/api/verification/repair',base_url=base,headers=headers,json={})
    assert response.status_code==400 and 'завершите проверку' in response.json['error']
    assert client.get('/api/verification?offset=-1',base_url=base,headers=headers).status_code==400


def test_recovery_does_not_block_initial_page_or_allow_conflicting_job(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/safe.bin',b'keep',preview=False)
    job=run_job(system,mode='fallback')
    with engine.store.db() as db:
        artifact=dict(db.execute('SELECT * FROM artifacts WHERE job=?',(job['id'],)).fetchone())
        db.execute('INSERT INTO pending_publications VALUES(?,?,?,?,?,?,?,?,?)',(job['id'],artifact['path'],'original','.disk-fakepart.part',artifact['local'],artifact['size'],artifact['sha256'],artifact['fingerprint'],None))
    from disk_downloader.recovery import digest_file
    entered,release=threading.Event(),threading.Event()
    def blocked(*args,**kwargs):
        entered.set()
        assert release.wait(test_timeout(10))
        return digest_file(*args,**kwargs)
    monkeypatch.setattr('disk_downloader.recovery.digest_file',blocked)
    recovery=None
    try:
        started=time.monotonic()
        app=create_app(Path(engine.store.filename).parent,api=api)
        recovery=app.extensions['recovery']
        assert time.monotonic()-started<test_timeout(2)
        assert entered.wait(test_timeout(5))
        assert app.test_client().get('/',base_url='http://127.0.0.1:8766').status_code==200
        with pytest.raises(ValueError,match='восстановления'):
            app.extensions['engine'].create({'destination':job['root'],'operation':'missing'})
    finally:
        release.set()
        if recovery and recovery.thread: recovery.thread.join(test_timeout(10))
    assert not recovery.snapshot()['busy'] and recovery.snapshot()['recovered']==1
    assert (Path(job['root'])/'safe.bin').read_bytes()==b'keep'
