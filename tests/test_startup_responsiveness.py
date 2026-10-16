import json
from pathlib import Path
import threading
import time

import pytest

from conftest import FakeAPI, make_job, wait_job, test_timeout
from test_api_security import web_session
from disk_downloader.folder_dialog import FolderPicker
from disk_downloader.management import Management
from disk_downloader.web import create_app


def test_folder_dialog_does_not_hold_http_thread(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    entered, release = threading.Event(), threading.Event()
    target = str(tmp_path/'Выбранная папка')
    def choose(initial):
        entered.set()
        assert release.wait(test_timeout(5))
        return target
    picker = app.extensions['folder_picker']
    picker.chooser = choose
    try:
        before = time.monotonic()
        result = client.post('/api/folder', base_url=base, headers=headers, json={'initial':str(tmp_path)})
        assert result.status_code == 200 and result.json['busy']
        assert time.monotonic()-before < 1
        assert entered.wait(test_timeout(2))
        assert client.get('/api/status', base_url=base, headers=headers).status_code == 200
        second = client.post('/api/folder', base_url=base, headers=headers, json={})
        assert second.json['request_id'] == result.json['request_id']
    finally:
        release.set()
        picker.thread.join(test_timeout(5))
    final = client.get('/api/folder',base_url=base,headers=headers)
    assert not final.json['busy'] and final.json['path'] == target


def test_failed_folder_dialog_allows_manual_and_in_app_choice(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    picker = app.extensions['folder_picker']
    def failed(initial):
        raise ModuleNotFoundError('tkinter')
    picker.chooser = failed
    client.post('/api/folder',base_url=base,headers=headers,json={})
    picker.thread.join(test_timeout(5))
    result = client.get('/api/folder',base_url=base,headers=headers).json
    assert not result['busy'] and 'полный путь' in result['error']
    folder = tmp_path/'Дозагрузка фотографий'
    folder.mkdir()
    result = client.get('/api/local-folders',query_string={'path':str(tmp_path)},base_url=base,headers=headers)
    assert result.status_code == 200
    assert {'name':folder.name,'path':str(folder)} in result.json['folders']
    assert client.get('/api/local-folders',base_url=base).status_code == 403


def test_status_is_read_only_and_never_waits_for_engine_lock(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions['engine']
    job_id = engine.create({'destination':str(tmp_path/'results'),'whole_disk':True,'auto_pause':False})
    wait_job(engine, job_id)
    entered, release = threading.Event(), threading.Event()
    def hold():
        with engine.lock:
            entered.set()
            release.wait(test_timeout(5))
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(test_timeout(2))
        start = time.monotonic()
        for url in ('/api/status','/api/jobs/'+job_id):
            assert client.get(url,base_url=base,headers=headers).status_code == 200
        assert time.monotonic()-start < 1
    finally:
        release.set()
        thread.join(test_timeout(5))


def test_slow_accounting_does_not_block_job_or_controls(tmp_path, monkeypatch):
    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions['engine']
    job_id = engine.create({'destination':str(tmp_path/'results'),'whole_disk':True,'auto_pause':False})
    wait_job(engine, job_id)
    entered, release = threading.Event(), threading.Event()
    original = engine.status
    def slow(key):
        entered.set()
        release.wait(test_timeout(5))
        return original(key)
    monkeypatch.setattr(engine, 'status', slow)
    try:
        start = time.monotonic()
        for _ in range(8):
            result = client.get('/api/jobs/'+job_id,base_url=base,headers=headers)
            assert result.status_code == 200 and result.json['statistics_pending']
        assert time.monotonic()-start < 1
        assert entered.wait(test_timeout(2))
        assert len(engine.status_cache.pending) == 1
        assert client.get('/api/status',base_url=base,headers=headers).json['active_job'] is None
    finally:
        release.set()


def test_connect_does_not_start_persisted_queue_after_restart(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions['engine']
    job_id = engine.create({'destination':str(tmp_path/'results'),'whole_disk':True,'auto_pause':False})
    wait_job(engine, job_id)
    manager = app.extensions['management']
    manager.queue_action({'action':'add','job':job_id})
    manager.armed = False  # New process: saved rows are present, consent isn't.
    engine.api.verify_token = lambda token: None
    engine.api.credentials.clear()
    response = client.post('/api/connect',base_url=base,headers=headers,json={'token':'fake-reconnect-test-token'})
    assert response.status_code == 200
    manager.tick()
    assert manager.queue_snapshot()['needs_confirmation']
    assert manager.queue_snapshot()['paused']
    assert not engine.threads[job_id].is_alive()
    assert client.get('/api/status',base_url=base,headers=headers).json['active_job'] is None
    manager.queue_action({'action':'resume'})
    manager.tick()
    assert wait_job(engine,job_id)['phase'] == 'completed'


@pytest.mark.parametrize('settings',[{}, {'mode':'bad'}, {'paths':None}, {'timeout':'broken'},
    {'paths':['disk:/'], 'exclusions':'bad'}, {'paths':['disk:/'], 'file_types':[{}]}])
def test_malformed_old_history_is_readable_and_does_not_crash_ui(tmp_path,settings):
    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions['engine']
    job_id = engine.create({'destination':str(tmp_path/'results'),'whole_disk':True,'auto_pause':False})
    wait_job(engine,job_id)
    engine.store.update_job(job_id,settings=json.dumps(settings))
    response = client.get('/api/jobs/'+job_id,base_url=base,headers=headers)
    assert response.status_code == 200 and response.json['settings']['_invalid']
    assert client.get('/api/status',base_url=base,headers=headers).status_code == 200
    failed = client.post('/api/jobs/'+job_id+'/action',base_url=base,headers=headers,json={'action':'resume'})
    assert failed.status_code == 400 and 'повреждены' in failed.json['error']
    assert client.get('/api/browse',base_url=base,headers=headers).status_code == 200


def test_journal_does_not_call_ui_status(system,monkeypatch):
    from disk_downloader.journal import Journal
    engine,api,tmp = system
    job_id = make_job(system)
    def invalid(*args):
        raise AssertionError('UI accounting must never run inside journal flush')
    monkeypatch.setattr(engine,'status',invalid)
    journal = Journal(engine,job_id)
    journal.flush()
    assert journal.saved and not journal.error


def test_internal_request_log_records_location_without_user_secrets(tmp_path,monkeypatch):
    app,client,base,headers = web_session(tmp_path)
    def failed():
        raise RuntimeError('private-token private-folder private-document')
    monkeypatch.setattr(app.extensions['store'],'db',failed)
    response = client.get('/api/status',base_url=base,headers=headers)
    assert response.status_code == 500
    records = app.extensions['engine'].request_errors.snapshot()
    assert records[-1]['request_id'] == response.json['request_id']
    assert records[-1]['exception'] == 'RuntimeError' and records[-1]['frames']
    text = (tmp_path/'state'/'application-errors.jsonl').read_text(encoding='utf-8')
    assert 'private-token' not in text and 'private-folder' not in text


def test_statistics_failure_is_visible_and_keeps_controls_available(system,monkeypatch):
    engine,api,tmp = system
    key = make_job(system)
    cache = engine.status_cache
    def failed(key):
        raise OSError('offline local drive')
    monkeypatch.setattr(engine,'status',failed)
    cache.refresh(key,(cache.generation,key))
    result = cache.snapshot(engine.store.job(key))
    assert result['statistics_pending'] and 'счётчики' in result['statistics_error']
    assert result['can_resume'] and not result['live']


def test_restore_discards_old_inflight_statistics(system,monkeypatch):
    engine,api,tmp = system
    key = make_job(system)
    cache = engine.status_cache
    value = engine.status(key)
    def old_statistics(key):
        cache.invalidate()  # History replaced while this calculation was running.
        return value
    monkeypatch.setattr(engine,'status',old_statistics)
    cache.refresh(key,(cache.generation,key))
    assert not cache.items and not cache.pending


def test_local_folder_listing_rejects_invalid_paths_and_handles_new_folder(tmp_path):
    app,client,base,headers = web_session(tmp_path)
    for path in ('relative/path','\0'):
        result = client.get('/api/local-folders',query_string={'path':path},base_url=base,headers=headers)
        assert result.status_code == 400
    result = client.get('/api/local-folders',query_string={'path':str(tmp_path/'not-created'/'yet')},base_url=base,headers=headers)
    assert result.status_code == 200 and result.json['missing'] and result.json['path'] == str(tmp_path)


def test_reconcile_does_not_overwrite_a_just_finished_job(system,monkeypatch):
    engine,api,tmp = system
    key = make_job(system)
    original = engine.store.job
    reads = 0
    def race(job_id):
        nonlocal reads
        reads += 1
        value = original(job_id)
        if reads == 1:
            engine.store.update_job(job_id,phase='completed')
            value['phase'] = 'downloading'
        return value
    monkeypatch.setattr(engine.store,'job',race)
    assert engine.reconcile(key)['phase'] == 'completed'
    assert original(key)['phase'] == 'completed'


def test_diagnostics_and_issue_draft_work_when_accounting_fails(system,monkeypatch):
    from disk_downloader.diagnostics import bundle
    from disk_downloader.issues import draft
    engine,api,tmp = system
    key = make_job(system)
    def failed(key):
        raise OSError('Unavailable statistics drive')
    monkeypatch.setattr(engine,'status',failed)
    report = bundle(engine,key)
    assert report['summary.json']['statistics_pending']
    assert report['summary.json']['files'] is None
    message = draft({'title':'Не грузится', 'description':'Не получается открыть список файлов.', 'job_id':key},engine,'murzirius/YaDiskDownloader')
    assert 'в фоне' in message['body'] and 'Сборка:' in message['body']
