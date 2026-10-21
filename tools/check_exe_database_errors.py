"""Check background database failures and recovery in the actual frozen EXE."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import requests

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from disk_downloader.store import Store
from disk_downloader.engine import Engine, fingerprint
from disk_downloader.api import DiskAPI, Credentials
from disk_downloader.import_state import FORMAT

parser=argparse.ArgumentParser()
parser.add_argument('--exe',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
parser.add_argument('--old',action='store_true')
args=parser.parse_args()
exe=args.exe.resolve(strict=True)

with tempfile.TemporaryDirectory(prefix='yadisk-database-diagnostics-') as directory:
    folder=Path(directory);root=folder/'results';root.mkdir();state=folder/'state'
    data=b'preserved-photo-placeholder';local='kept.bin';path='disk:/kept.bin'
    sha=hashlib.sha256(data).hexdigest();(root/local).write_bytes(data)
    store=Store(state/'jobs.sqlite3')
    settings=Engine(store,DiskAPI(Credentials())).validate_settings({
        'destination':str(root),'whole_disk':True,'auto_pause':False,
        'mode':'previews','workers':15,'reports':['html'],'create_zip':False})
    meta={'path':path,'name':local,'type':'file','size':len(data),'sha256':sha}
    manifest=root/'.disk-import.sqlite3'
    with closing(sqlite3.connect(manifest)) as db, db:
        db.executescript('CREATE TABLE import_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE names(identity TEXT PRIMARY KEY,local TEXT); CREATE TABLE artifacts(path TEXT,type TEXT,local TEXT,size INTEGER,sha256 TEXT,fingerprint TEXT,preview_size TEXT); CREATE TABLE inventory(path TEXT PRIMARY KEY,kind TEXT,meta TEXT);')
        db.executemany('INSERT INTO import_meta VALUES(?,?)',[
            ('format',FORMAT),('settings',json.dumps(settings)),('service','__service'),('generation','0')])
        db.execute('INSERT INTO names VALUES(?,?)',(path,local))
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)',(path,'original',local,len(data),sha,fingerprint(meta),None))
        db.execute('INSERT INTO inventory VALUES(?,?,?)',(path,'file',json.dumps(meta)))
    before=hashlib.sha256(manifest.read_bytes()).hexdigest()
    settings=dict(settings,operation='missing',_inherit_source={'manifest':manifest.name})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('test-target',json.dumps(settings),str(root),'__service','interrupted','2026-01-01'))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    env={key:value for key,value in os.environ.items() if not key.startswith('PYTHON')}
    env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
    process=subprocess.Popen([str(exe),'--no-browser','--port',str(port),'--state-dir',str(state)],cwd=folder,env=env)
    session=requests.Session();session.trust_env=False;headers={}
    try:
        due=time.monotonic()+30
        while time.monotonic()<due:
            assert process.poll() is None,'EXE exited at startup'
            try:
                response=session.get(url+'/',timeout=1)
                if response.status_code==200:break
            except requests.RequestException:pass
            time.sleep(.1)
        else:raise AssertionError('Startup timed out')
        if not args.old:assert 'id="open-diagnostics"' in response.text
        headers={'Origin':url,'X-CSRF-Token':re.search(r'name="csrf-token" content="([^"]+)"',response.text).group(1)}
        def get(suffix):
            response=session.get(url+suffix,headers=headers,timeout=5);response.raise_for_status();return response.json()
        def action():
            response=session.post(url+'/api/jobs/test-target/action',headers=headers,json={'action':'save_manifest'},timeout=5)
            response.raise_for_status()
            due=time.monotonic()+30
            while time.monotonic()<due:
                result=get('/api/jobs/test-target')
                if not result['live']:return result
                time.sleep(.05)
            raise AssertionError('Task remained active after database failure')
        runtime=get('/api/runtime');failed=action()
        assert failed['phase']=='failed' and failed['can_resume'],failed.get('error')
        files=get('/api/jobs/test-target/diagnostics-preview')['files']
        records=files['requests.json']
        if args.old:
            assert not records
            assert failed['error']=='Ошибка базы истории (SQLITE_ERROR)'
        else:
            assert len(records)==1,records
            record=records[0]
            assert record['sqlite']['reason']=='missing_column',record
            assert record['sqlite']['code']=='SQLITE_ERROR'
            assert record['phase']=='preparing' and record['operation']=='job_manifest'
            assert record['request_id'] in failed['error']
            assert any(frame['file']=='import_state.py' for frame in record['frames'])
            assert files['summary.json']['error_type']=='local_database'
            assert str(folder) not in json.dumps(files)
        assert (root/local).read_bytes()==data
        assert hashlib.sha256(manifest.read_bytes()).hexdigest()==before
        # Repair only the deliberately malformed synthetic fixture. Retrying
        # must keep the interrupted import resumable and preserve its copy.
        with closing(sqlite3.connect(manifest)) as db, db:
            db.execute('ALTER TABLE names ADD COLUMN folded TEXT')
            db.execute('UPDATE names SET folded=local')
        resumed=action()
        assert resumed['phase']=='stopped' and not resumed['settings'].get('_inherit_source'),resumed.get('error')
        assert (root/local).read_bytes()==data
        with store.db() as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            assert db.execute("SELECT COUNT(*) FROM artifacts WHERE job='test-target'").fetchone()[0]==1
        result={'build':runtime['build'],'actual_exe':True,'synthetic_sqlite_error':True,
                'background_records':len(records),'cause':records[0]['sqlite']['reason'] if records else None,
                'files_preserved':True,'retry_after_fixture_repair':True,'quick_check':'ok'}
        response=session.post(url+'/api/exit',headers=headers,json={},timeout=5);response.raise_for_status()
        process.wait(timeout=20);assert process.returncode==0
        args.output.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
    finally:
        if process.poll() is None:
            if headers:
                try:session.post(url+'/api/exit',headers=headers,json={},timeout=5);process.wait(timeout=20)
                except (requests.RequestException,subprocess.TimeoutExpired):pass
            if process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                process.wait(timeout=10)
        session.close()
