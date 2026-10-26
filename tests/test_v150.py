import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from conftest import FakeAPI, wait_job, test_timeout
from disk_downloader.folder_stats import FolderStats
from disk_downloader.selection_rules import included, validate
from disk_downloader.work_policy import due, quiet
from disk_downloader.web import create_app


def finish_stats(stats):
    stats.thread.join(test_timeout(10))
    assert not stats.thread.is_alive()
    return stats.snapshot()


def test_folder_totals_use_descendant_files_and_latest_timestamp():
    api=FakeAPI()
    api.file('disk:/photos/nested/old.jpg',data=b'old')
    api.file('disk:/photos/new.jpg',data=b'newer')
    api.directory('disk:/empty')
    api.tree['disk:/photos']['modified']='2099-01-01T00:00:00Z'
    api.tree['disk:/photos/nested/old.jpg']['modified']='2026-10-20T13:00:00+03:00'
    api.tree['disk:/photos/new.jpg']['modified']='2026-10-20T11:00:00Z'
    stats=FolderStats(api);stats.start(['disk:/photos','disk:/empty'])
    result=finish_stats(stats)['items']
    assert result['disk:/photos']['files']==2 and result['disk:/photos']['bytes']==8
    assert result['disk:/photos']['latest_modified']=='2026-10-20T11:00:00+00:00'
    assert result['disk:/photos']['complete']
    assert result['disk:/empty']['bytes']==0 and result['disk:/empty']['latest_modified'] is None
    api.credentials.clear()
    assert stats.snapshot()['items']=={}


def test_repeated_folder_page_does_not_hang_or_claim_complete():
    api=FakeAPI();api.file('disk:/one.jpg')
    original=api.metadata
    def repeated(*args,**kwargs):
        kwargs['offset']=0
        result=original(*args,**kwargs)
        result['_embedded']['total']=100000
        return result
    api.metadata=repeated
    stats=FolderStats(api);stats.start(['disk:/'])
    result=finish_stats(stats)['items']['disk:/']
    assert not result['complete'] and result['files']==1
    assert len(api.metadata_calls)==2


def test_folder_count_cancels_and_rejects_concurrent_request():
    api=FakeAPI();api.directory('disk:/photos')
    entered=threading.Event();original=api.metadata
    def blocked(path,settings=None,checkpoint=lambda:None,**kwargs):
        entered.set()
        while True:checkpoint();time.sleep(.01)
    api.metadata=blocked;stats=FolderStats(api);stats.start(['disk:/photos'])
    assert entered.wait(test_timeout(3))
    with pytest.raises(ValueError):stats.start(['disk:/photos'])
    stats.cancel.set()
    assert finish_stats(stats)['error']


def test_browse_never_presents_folder_own_modified_as_file_date(tmp_path):
    api=FakeAPI();api.directory('disk:/photos');api.tree['disk:/photos']['modified']='2099-01-01T00:00:00Z'
    app=create_app(tmp_path/'state',api=api);client=app.test_client()
    with client.session_transaction(base_url='http://127.0.0.1:8766') as session:session['csrf']='test'
    response=client.get('/api/browse',base_url='http://127.0.0.1:8766',headers={'X-CSRF-Token':'test'})
    assert response.status_code==200
    folder=response.json['items'][0]
    assert folder['modified'] is None and folder['size'] is None


def test_parallel_scan_has_bounded_requests_and_complete_results(system):
    engine,api,tmp=system
    for folder in range(12):api.file(f'disk:/folder-{folder:02}/one.bin',preview=False)
    lock=threading.Lock();running=0;peak=0;original=api.metadata
    def metadata(*args,**kwargs):
        nonlocal running,peak
        with lock:running+=1;peak=max(peak,running)
        try:time.sleep(.02);return original(*args,**kwargs)
        finally:
            with lock:running-=1
    api.metadata=metadata
    key=engine.create({'whole_disk':True,'destination':str(tmp/'results'),'create_zip':False})
    result=wait_job(engine,key)
    assert result['phase']=='ready' and result['files']==12
    assert 1<peak<=4


def test_scan_repeated_page_reports_error_without_infinite_loop(system):
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False);original=api.metadata
    def metadata(*args,**kwargs):
        if kwargs.get('offset',0):kwargs['offset']=0
        result=original(*args,**kwargs)
        if kwargs.get('limit')==1000:result['_embedded']['total']=100000
        return result
    api.metadata=metadata
    key=engine.create({'whole_disk':True,'destination':str(tmp/'results'),'create_zip':False})
    result=wait_job(engine,key)
    assert result['scan_errors']==1 and result['files']==1
    assert len(api.metadata_calls)<5


def test_scan_alternating_repeated_pages_terminates(system):
    engine,api,tmp=system;api.file('disk:/a.bin',preview=False);api.file('disk:/b.bin',preview=False)
    original=api.metadata
    def metadata(*args,**kwargs):
        result=original(*args,**kwargs)
        if kwargs.get('limit')==1000:
            result['_embedded']={'items':[copy.deepcopy(api.tree['disk:/a.bin' if kwargs.get('offset',0)%2==0 else 'disk:/b.bin'])]}
        return result
    api.metadata=metadata
    key=engine.create({'whole_disk':True,'destination':str(tmp/'results'),'create_zip':False})
    result=wait_job(engine,key)
    assert result['scan_errors']==1 and result['files']==2


def test_priorities_preserve_no_copy_first_and_apply_within_group(system):
    engine,api,tmp=system
    api.file('disk:/a/first.bin',preview=False);api.file('disk:/z/important.bin',preview=False)
    key=engine.create({'whole_disk':True,'destination':str(tmp/'results'),'mode':'fallback','workers':1,'create_zip':False,'priority_paths':['disk:/z']})
    wait_job(engine,key);engine.action(key,'start');result=wait_job(engine,key)
    assert result['phase']=='completed'
    assert [path for path,kind in api.media_calls]==['disk:/z/important.bin','disk:/a/first.bin']
    with engine.store.db() as db:
        plan=db.execute("EXPLAIN QUERY PLAN SELECT * FROM entries WHERE job=? AND kind='file' AND status='pending' ORDER BY priority,user_priority,path LIMIT 8",(key,)).fetchall()
    assert not any('TEMP B-TREE' in str(tuple(row)) for row in plan)


def test_live_limits_change_actual_concurrency(system):
    engine,api,tmp=system
    for i in range(7):api.file(f'disk:/{i}.bin',preview=False)
    key=engine.create({'whole_disk':True,'destination':str(tmp/'results'),'mode':'fallback','workers':1,'create_zip':False})
    wait_job(engine,key);api.delay=1;api.block=threading.Event();engine.action(key,'start')
    assert api.block.wait(test_timeout(5))
    assert len(api.media_calls)==1
    engine.live_limits(key,{'workers':3,'speed_limit_mib':0})
    deadline=time.monotonic()+test_timeout(5)
    while len(api.media_calls)<3 and time.monotonic()<deadline:time.sleep(.02)
    assert len(api.media_calls)==3
    engine.action(key,'stop');api.delay=0
    wait_job(engine,key)


@pytest.mark.parametrize('data',[{'date_from':'2026-02-30'},{'date_from':'2026-10-01','date_to':'2026-09-01'},{'date_field':'anything'},{'priority_paths':['disk:/../bad']},{'include_unknown_dates':'yes'}])
def test_invalid_date_and_priority_rules(data):
    with pytest.raises(ValueError):validate(data)


def test_dates_and_missing_dates_are_explicit():
    settings=validate({'date_from':'2026-01-01','date_to':'2026-12-31','date_field':'taken'})
    assert included({'exif':{'date_time':'2026-06-01T10:00:00Z'}},settings)
    assert not included({'taken':'2025-12-31T00:00:00Z'},settings)
    assert included({},settings)
    assert not included({},settings|{'include_unknown_dates':False})


def test_schedule_and_quiet_hours():
    settings={'run_at':'2026-10-21T18:00:00+03:00','quiet_hours':{'enabled':True,'start':23,'end':8}}
    assert not due(settings,datetime(2026,10,21,14,59,tzinfo=timezone.utc))
    assert due(settings,datetime(2026,10,21,15,tzinfo=timezone.utc))
    assert quiet(settings,23) and quiet(settings,7) and not quiet(settings,8) and not quiet(settings,12)


def test_rate_limit_changes_release_wait_without_deadlock():
    from disk_downloader.rate_limit import RateLimiter
    limiter=RateLimiter(.001);done=threading.Event()
    thread=threading.Thread(target=lambda:(limiter.consume(1024*1024,lambda:None),done.set()),daemon=True)
    thread.start();time.sleep(.02);limiter.configure(0)
    assert done.wait(test_timeout(2));thread.join()


@pytest.mark.skipif(sys.platform != 'linux', reason='The public CLI is Linux-only')
def test_cli_background_survives_its_launch_terminal_process(tmp_path):
    from disk_downloader.cli import Client
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    command=[sys.executable,'-m','disk_downloader','--port',str(port),'--state-dir',str(tmp_path/'state'),'--json','start']
    result=subprocess.run(command,capture_output=True,text=True,timeout=test_timeout(50))
    assert result.returncode==0,result.stderr
    process=None
    try:
        import psutil
        process=psutil.Process(json.loads(result.stdout)['pid'])
        client=Client(port,tmp_path/'state')
        from disk_downloader import __version__
        assert client.call('/api/runtime')['version']==__version__
        assert Client(port,tmp_path/'state').call('/api/status')['jobs']==[]
        assert client.call('/api/exit',{})['ok']
        due_at=time.monotonic()+test_timeout(20)
        while time.monotonic()<due_at:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1',port))!=0:break
            time.sleep(.1)
        else:pytest.fail('Background server did not release its port')
    finally:
        if process and process.is_running():
            process.terminate()
            try:process.wait(timeout=5)
            except psutil.TimeoutExpired:process.kill();process.wait(timeout=5)


def test_theme_icons_packaged_and_cli_has_no_token_argument(monkeypatch):
    root=Path(__file__).resolve().parents[1]
    import importlib.util
    spec=importlib.util.spec_from_file_location('package',root/'tools/package_release.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert root/'disk_downloader/static/file-icons.svg' in set(module.source_files())
    from disk_downloader.cli import main
    monkeypatch.setattr(sys,'platform','linux')
    with pytest.raises(SystemExit):main(['connect','--token','never-pass-secret-in-arguments'])


def test_exclusions_and_date_filters_have_explanations(system):
    from disk_downloader.insights import files
    engine,api,tmp=system
    api.file('disk:/keep.jpg');api.file('disk:/skip.tmp');api.directory('disk:/excluded')
    api.file('disk:/old.jpg');api.tree['disk:/old.jpg']['modified']='2025-01-01T00:00:00Z'
    key=engine.create({'whole_disk':True,'destination':str(tmp/'out'),'date_from':'2026-01-01','exclusions':['*.tmp','disk:/excluded'],'create_zip':False})
    result=wait_job(engine,key)
    assert result['files']==1
    items=files(engine.store,key,state='skipped')['items']
    assert {item['path'] for item in items}=={'disk:/old.jpg','disk:/skip.tmp','disk:/excluded'}
    assert all(item['reason'] and item['status']=='excluded' for item in items)


def test_unsupported_preview_does_not_enter_its_decoder(monkeypatch):
    import io
    from PIL import Image, UnidentifiedImageError, PpmImagePlugin
    from disk_downloader.image_security import PREVIEW_FORMATS
    def forbidden(*_):raise AssertionError('Unnecessary decoder reached')
    monkeypatch.setattr(PpmImagePlugin.PpmImageFile,'_open',forbidden)
    with pytest.raises(UnidentifiedImageError):
        Image.open(io.BytesIO(b'P6\n1 1\n255\n\x00\x00\x00'),formats=PREVIEW_FORMATS)


def test_invalid_preview_error_identifies_source():
    from PIL import UnidentifiedImageError
    from disk_downloader.engine import safe_error
    text=safe_error(UnidentifiedImageError('invalid image'))
    assert 'Яндекс.Диск' in text and 'превью' in text and 'файловой системы' not in text


def test_unicode_local_names_fit_windows_and_unix_component_limits():
    from disk_downloader.paths import component
    for value in ['照'*120+'.jpg','🖼'*150+'.png','e\u0301.jpg']:
        name=component(value,'disk:/'+value)
        assert len(name.encode('utf-8'))<255 and len(name.encode('utf-16-le'))<=240
    assert component('é.jpg','disk:/é.jpg')!=component('e\u0301.jpg','disk:/e\u0301.jpg')


@pytest.mark.parametrize('code',[-2147221164,0x80040154])
def test_failed_windows_folder_dialog_is_reported_before_null_com_call(code):
    from disk_downloader.folder_dialog import check_hresult
    with pytest.raises(OSError,match='HRESULT'):check_hresult(code,'создать окно выбора папки')


def test_queue_waits_for_scheduled_start(system):
    from disk_downloader.management import Management
    engine,api,tmp=system;api.file('disk:/one.bin',preview=False)
    key=engine.create({'whole_disk':True,'destination':str(tmp/'out'),'create_zip':False,'run_at':'2099-01-01T00:00:00Z'})
    wait_job(engine,key)
    manager=Management(engine);manager.queue_action({'action':'add','job':key});manager.tick()
    assert engine.store.job(key)['phase']=='ready'
    assert not engine.threads[key].is_alive() and not api.media_calls
    assert manager.queue_snapshot()['items'][0]['run_at']=='2099-01-01T00:00:00Z'
    assert manager.next_due>time.time()


def test_stop_clears_old_auto_pause_message():
    from disk_downloader.engine import Control
    control=Control();control.pause_reason='Autopause';control.pause();control.stop()
    assert control.stopped and not control.paused and not control.pause_reason


@pytest.mark.skipif(os.name!='nt',reason='Windows short path alias')
def test_state_identity_accepts_windows_short_path_alias(tmp_path):
    import ctypes
    from disk_downloader.single_instance import state_key
    directory=tmp_path/'state';directory.mkdir()
    buffer=ctypes.create_unicode_buffer(32768)
    result=ctypes.windll.kernel32.GetShortPathNameW(str(directory),buffer,len(buffer))
    assert result>0
    assert state_key(directory)==state_key(buffer.value)


def test_cli_connect_timeout_still_signals_missing_server(monkeypatch,tmp_path):
    import requests
    from disk_downloader.cli import Client
    def timeout(*args,**kwargs):raise requests.ConnectTimeout('test')
    monkeypatch.setattr(requests.Session,'get',timeout)
    with pytest.raises(requests.ConnectionError):Client(12345,tmp_path)


def test_system_helpers_restore_library_environment(monkeypatch):
    from disk_downloader import runtime
    monkeypatch.setattr(runtime,'frozen',lambda:True)
    monkeypatch.setenv('LD_LIBRARY_PATH','/bundle-libraries')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG','/system-libraries')
    monkeypatch.setenv('DYLD_LIBRARY_PATH','/bundle-mac-libraries')
    monkeypatch.delenv('DYLD_LIBRARY_PATH_ORIG',raising=False)
    env=runtime.external_environment()
    assert env['LD_LIBRARY_PATH']=='/system-libraries' and 'DYLD_LIBRARY_PATH' not in env


def test_frozen_background_process_has_independent_resources(monkeypatch,tmp_path):
    import requests
    from disk_downloader import service_control as cli
    calls=[];options=[]
    def client(*args):
        calls.append(args)
        if len(calls)==1:raise requests.ConnectionError()
        return None
    class Process:
        pid=123
        def poll(self):return None
    def popen(command,**kwargs):options.append(kwargs);return Process()
    monkeypatch.setattr(cli,'Client',client);monkeypatch.setattr(cli,'frozen',lambda:True);monkeypatch.setattr(cli.subprocess,'Popen',popen)
    assert cli.start_background(12345,tmp_path)['running']
    assert options[0]['env']['PYINSTALLER_RESET_ENVIRONMENT']=='1'


def test_folder_missing_file_date_does_not_claim_exact_latest():
    api=FakeAPI();api.file('disk:/photos/dates.jpg');api.file('disk:/photos/undated.jpg')
    api.tree['disk:/photos/undated.jpg']['modified']=None
    stats=FolderStats(api);stats.start(['disk:/photos'])
    value=finish_stats(stats)['items']['disk:/photos']
    assert value['files']==2 and value['complete']
    assert value['latest_modified'] is not None and not value['date_complete']


@pytest.mark.parametrize('system',['darwin','linux'])
def test_native_unix_uses_rollback_when_wal_is_not_patched(monkeypatch,system):
    from disk_downloader import sqlite_support
    monkeypatch.setattr(sys,'platform',system)
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sqlite_support,'safe_wal',lambda:False)
    assert sqlite_support.journal_mode()=='DELETE'
