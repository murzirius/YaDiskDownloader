import hashlib
import threading
import time
from pathlib import Path

import pytest

from conftest import run_job,wait_job
from disk_downloader.api import APIError
from disk_downloader.engine import Control
from disk_downloader.insights import DestinationChecks,download_plan,files


def resume_folder(engine, root, policy='originals'):
    job=engine.create({'destination':str(root),'operation':'missing','whole_disk':True,'missing_policy':policy,'mode':'fallback','reports':['json'],'create_zip':False})
    assert wait_job(engine,job)['phase']=='ready'
    engine.action(job,'start')
    return wait_job(engine,job)


def test_upgrade_removes_only_verified_preview(system):
    engine,api,_=system;api.file('disk:/photo.jpg')
    first=run_job(system);root=Path(first['root']);preview=root/'photo.jpg.preview.png'
    assert preview.exists()
    result=resume_folder(engine,root)
    assert result['originals']==1 and not preview.exists()
    assert (root/'photo.jpg').read_bytes()==b'original'


def test_modified_preview_is_preserved(system):
    engine,api,_=system;api.file('disk:/photo.jpg')
    first=run_job(system);root=Path(first['root']);preview=root/'photo.jpg.preview.png'
    preview.write_bytes(b'changed by another application')
    result=resume_folder(engine,root)
    assert result['originals']==1 and preview.read_bytes()==b'changed by another application'


def test_cleanup_keeps_preview_when_original_changed(system):
    engine,api,_=system;api.file('disk:/photo.jpg')
    first=run_job(system,mode='both');root=Path(first['root'])
    (root/'photo.jpg').write_bytes(b'damaged!')
    engine.remove_preview(engine.store.job(first['id']),'disk:/photo.jpg',Control())
    assert (root/'photo.jpg.preview.png').exists()


def test_existing_original_does_not_adopt_modified_preview(system):
    engine,api,_=system;api.file('disk:/photo.jpg')
    first=run_job(system,mode='both');root=Path(first['root'])
    preview=root/'photo.jpg.preview.png';preview.write_bytes(b'foreign edit')
    result=resume_folder(engine,root)
    assert preview.read_bytes()==b'foreign edit' and result['originals']==1 and result['previews']==0


def test_service_names_stay_reserved_without_inspecting_log_files(system,monkeypatch):
    engine,api,_=system;api.file('disk:/one.bin',preview=False)
    first=run_job(system,mode='fallback',reports=['json']);root=Path(first['root'])
    report=(root/'report.json').read_bytes()
    api.file('disk:/report.json',report,preview=False);api.media_calls.clear()
    import disk_downloader.import_state as module
    original=module.safe_join
    def checked(root,relative):
        assert relative!='report.json','Import must reserve a report name without inspecting its file'
        return original(root,relative)
    monkeypatch.setattr(module,'safe_join',checked)
    result=resume_folder(engine,root)
    row=next(engine.store.rows("SELECT local FROM artifacts WHERE job=? AND path='disk:/report.json'",(result['id'],)))
    assert row['local']!='report.json'
    assert (root/row['local']).read_bytes()==report
    assert ('disk:/report.json','original') in api.media_calls


def test_grouped_errors_distinguish_cloud_and_local_files(system):
    from disk_downloader.insights import problems
    engine,api,_=system;api.file('disk:/one.jpg');first=run_job(system)
    engine.store.update_entry(first['id'],'disk:/one.jpg',error='Оригинал: доступ запрещён')
    assert problems(engine.store,first['id'])['groups'][0]['source']=='Яндекс.Диск'
    engine.store.update_entry(first['id'],'disk:/one.jpg',error='Windows запретила запись файла')
    assert problems(engine.store,first['id'])['groups'][0]['source']=='Файлы и накопитель на устройстве'


def test_both_policy_fetches_preview_for_existing_original(system):
    engine,api,_=system;api.file('disk:/photo.jpg')
    first=run_job(system,mode='fallback');root=Path(first['root']);api.media_calls.clear()
    result=resume_folder(engine,root,'both')
    assert result['originals']==1 and result['previews']==1
    assert api.media_calls==[('disk:/photo.jpg','preview')]
    assert (root/'photo.jpg.preview.png').exists()
    api.media_calls.clear();second=resume_folder(engine,root,'both')
    assert second['originals']==1 and second['previews']==1 and not api.media_calls


def test_failed_original_keeps_preview(system):
    engine,api,_=system;api.file('disk:/photo.jpg')
    first=run_job(system);root=Path(first['root']);preview=root/'photo.jpg.preview.png'
    before=preview.read_bytes();api.fail[('disk:/photo.jpg','original')]=[APIError('Unavailable',503)]*3
    result=resume_folder(engine,root)
    assert result['originals']==0 and preview.read_bytes()==before


def test_local_check_never_hashes_service_logs(system,monkeypatch):
    engine,api,_=system;api.file('disk:/report.json',preview=False)
    first=run_job(system,mode='fallback');root=Path(first['root'])
    (root/'application.log').write_bytes(b'not downloaded')
    import disk_downloader.engine as module
    original=module.file_hash;visited=[]
    def tracked(filename,checkpoint=lambda:None):
        visited.append(Path(filename).name);return original(filename,checkpoint)
    monkeypatch.setattr(module,'file_hash',tracked)
    result=resume_folder(engine,root)
    assert result['originals']==1 and 'application.log' not in visited
    assert (root/'report.json').read_bytes()==b'original'


def test_fast_and_full_checks_preserve_integrity(system,monkeypatch):
    engine,api,_=system;api.file('disk:/one.bin',preview=False)
    first=run_job(system,mode='fallback');job=engine.store.job(first['id'])
    fast=Control();engine.check_local_results(job,fast)
    assert fast.verification_cached==1 and fast.verification_hashed==0
    job['settings']['verification_mode']='full';full=Control();engine.check_local_results(job,full)
    assert full.verification_cached==0 and full.verification_hashed==1
    file=Path(job['root'])/'one.bin';file.write_bytes(b'corrupt!')
    changed=Control();engine.check_local_results(job,changed)
    assert changed.verification['originals']==0


def test_file_search_is_literal_and_plan_is_bounded(system):
    engine,api,_=system;api.file('disk:/100%_Фото.jpg');api.file('disk:/other.jpg')
    job=run_job(system)
    found=files(engine.store,job['id'],'%_фото')
    assert [r['path'] for r in found['items']]==['disk:/100%_Фото.jpg']
    plan=download_plan(engine.store,job['id'])
    assert plan['files']==2 and plan['preview_only']==2


def test_destination_check_does_not_block_caller(system,monkeypatch):
    engine,_,_=system;entered=threading.Event();release=threading.Event()
    def inspect(store,path):entered.set();release.wait(5);return {'path':path}
    monkeypatch.setattr('disk_downloader.insights.inspect_destination',inspect)
    checker=DestinationChecks(engine.store)
    started=time.monotonic();result=checker.start('some-path')
    assert time.monotonic()-started<1 and result['busy'] and entered.wait(1)
    try:
        with pytest.raises(ValueError):checker.start('another-path')
    finally:release.set()


def test_twenty_workers_supported_with_eight_default(system):
    engine,_,tmp=system;base={'destination':str(tmp),'whole_disk':True}
    assert engine.validate_settings(base)['workers']==8
    assert engine.validate_settings(base|{'workers':20})['workers']==20
    with pytest.raises(ValueError):engine.validate_settings(base|{'workers':21})


def test_new_routes_enforce_csrf_and_literal_queries(tmp_path):
    from test_api_security import web_session
    app,client,base,headers=web_session(tmp_path)
    assert client.post('/api/destination-check',base_url=base,json={'path':str(tmp_path)}).status_code==403
    response=client.post('/api/destination-check',base_url=base,headers=headers,json={'path':str(tmp_path)})
    assert response.status_code==200 and response.json['id']
    engine=app.extensions['engine'];api=engine.api;api.file('disk:/100%_ФОТО.jpg')
    job=engine.create({'destination':str(tmp_path/'out'),'whole_disk':True,'mode':'previews','create_zip':False})
    assert wait_job(engine,job)['phase']=='ready'
    assert client.get('/api/jobs/'+job+'/files',base_url=base).status_code==403
    found=client.get('/api/jobs/'+job+'/files?q=%25_%D1%84%D0%BE%D1%82%D0%BE',base_url=base,headers=headers)
    assert found.status_code==200 and len(found.json['items'])==1
    for offset in ('-1','oops','999999999'):
        assert client.get('/api/jobs/'+job+'/files?offset='+offset,base_url=base,headers=headers).status_code==400
    plan=client.get('/api/jobs/'+job+'/plan',base_url=base,headers=headers)
    assert plan.status_code==200 and plan.json['known_bytes']==0
    assert client.post('/api/jobs/'+job+'/open-file-folder',base_url=base,json={'path':'disk:/100%_ФОТО.jpg'}).status_code==403


def test_file_search_pages_and_twenty_workers_actually_download(system):
    engine,api,tmp=system
    for i in range(105):api.file(f'disk:/{i:03}.bin',preview=False)
    job=engine.create({'destination':str(tmp/'out'),'whole_disk':True,'workers':20,'mode':'fallback','create_zip':False})
    assert wait_job(engine,job)['phase']=='ready'
    first=files(engine.store,job);second=files(engine.store,job,offset=100)
    assert len(first['items'])==100 and first['has_more'] and len(second['items'])==5 and not second['has_more']
    engine.action(job,'start');result=wait_job(engine,job)
    assert result['originals']==105 and result['errors']==0
