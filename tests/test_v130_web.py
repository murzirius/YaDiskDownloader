import io
import time
from pathlib import Path

import pytest
from test_api_security import web_session
from conftest import wait_job


def test_new_endpoints_keep_csrf_and_origin_protection(tmp_path):
    app,client,base,headers=web_session(tmp_path)
    for url in ('/api/history','/api/queue','/api/verification','/api/backups','/api/notifications','/api/update-install'):
        assert client.get(url,base_url=base).status_code==403
        assert client.get(url,base_url=base,headers=headers).status_code==200
    for url in ('/api/queue','/api/verification','/api/cleanup','/api/backups/restore','/api/notifications','/api/update-install'):
        assert client.post(url,base_url=base,json={}).status_code==403


def test_history_queue_and_offline_verification_routes(tmp_path):
    app,client,base,headers=web_session(tmp_path)
    app.extensions['disk_api'].file('disk:/check.bin',preview=False)
    engine=app.extensions['engine']
    response=client.post('/api/jobs',base_url=base,headers=headers,json={'destination':str(tmp_path/'results'),'whole_disk':True,'mode':'fallback','create_zip':False})
    job=response.get_json()['id'];wait_job(engine,job)
    assert client.post('/api/jobs/'+job+'/history',base_url=base,headers=headers,json={'action':'rename','title':'Test history'}).status_code==200
    assert client.get('/api/history?query=Test',base_url=base,headers=headers).get_json()['total']==1
    assert client.post('/api/queue',base_url=base,headers=headers,json={'action':'add','job':job}).status_code==200
    assert client.post('/api/jobs/'+job+'/history',base_url=base,headers=headers,json={'action':'archive'}).status_code==400
    assert client.post('/api/queue',base_url=base,headers=headers,json={'action':'remove','job':job}).status_code==200
    engine.action(job,'start');wait_job(engine,job)
    app.extensions['disk_api'].credentials.clear()
    assert client.post('/api/verification',base_url=base,headers=headers,json={'job':job}).status_code==200
    app.extensions['maintenance'].thread.join(5)
    assert client.get('/api/verification',base_url=base,headers=headers).get_json()['counts'].get('ok')


def test_backup_export_import_restore_and_corruption(tmp_path):
    app,client,base,headers=web_session(tmp_path)
    name=client.post('/api/backups',base_url=base,headers=headers,json={}).get_json()['name']
    data=client.get('/api/backups/'+name+'/download',base_url=base,headers=headers)
    assert data.status_code==200 and data.data.startswith(b'SQLite format 3')
    copied=client.post('/api/backups/import',base_url=base,headers=headers,data={'file':(io.BytesIO(data.data),'copy.sqlite3')},content_type='multipart/form-data')
    assert copied.status_code==200
    bad=client.post('/api/backups/import',base_url=base,headers=headers,data={'file':(io.BytesIO(b'broken'),'copy.sqlite3')},content_type='multipart/form-data')
    assert bad.status_code==400
    assert client.post('/api/backups/restore',base_url=base,headers=headers,json={'name':name}).status_code==400
    assert client.post('/api/backups/restore',base_url=base,headers=headers,json={'name':name,'confirm':True}).status_code==200
    assert not app.extensions['disk_api'].credentials.connected
    assert app.extensions['management'].paused()


def test_update_rejects_missing_asset_without_ending_app(tmp_path):
    app,client,base,headers=web_session(tmp_path)
    assert client.post('/api/update-install',base_url=base,headers=headers,json={'action':'download'}).status_code==400
    assert client.post('/api/update-install',base_url=base,headers=headers,json={'action':'install'}).status_code==400
    assert not app.extensions['engine'].shutting_down


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows recycle bin')
def test_modern_recycle_moves_only_own_test_file(tmp_path):
    from disk_downloader.maintenance import recycle
    file=tmp_path/'only-test-file.part';file.write_bytes(b'disposable test')
    recycle(file)
    assert not file.exists()
