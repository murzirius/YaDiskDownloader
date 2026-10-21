"""Reproduce unreadable history text and verify recovery in the real EXE."""
import argparse,hashlib,json,os,re,socket,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path
import requests

repo=Path(__file__).resolve().parents[1];base=repo
sys.path.insert(0,str(repo))
from disk_downloader.store import Store
from disk_downloader.engine import Engine,fingerprint
from disk_downloader.api import DiskAPI,Credentials

parser=argparse.ArgumentParser();parser.add_argument('--portable',action='store_true');parser.add_argument('--old',action='store_true');parser.add_argument('--exe',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
exe=args.exe.resolve(strict=True)
count=1000;body=b'kept';sha=hashlib.sha256(body).hexdigest()
with tempfile.TemporaryDirectory(prefix='yadisk-history-exe-') as directory:
    folder=Path(directory);root=folder/'results';root.mkdir();state=folder/'state'
    store=Store(state/'jobs.sqlite3')
    settings=Engine(store,DiskAPI(Credentials())).validate_settings({'destination':str(root),'whole_disk':True,'auto_pause':False,'reports':['json']})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES(?,?,?,?,?,?,?)',('history-source',json.dumps(settings),str(root),'__service','completed','2026-01-01',1))
        store.insert_entry(db,'history-source',{'path':'disk:/','type':'dir','name':'Disk'},settings)
        db.execute('INSERT INTO names VALUES(?,?,?,?)',('history-source','@service','__service','__service'))
        for index in range(count):
            name=f'file-{index:04}.bin';path='disk:/'+name;(root/name).write_bytes(body)
            meta={'path':path,'type':'file','name':name,'size':len(body),'sha256':sha,'media_type':'unknown','mime_type':'application/octet-stream'}
            store.insert_entry(db,'history-source',meta,settings)
            db.execute('INSERT INTO names VALUES(?,?,?,?)',('history-source',path,name,name))
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',('history-source',path,'original',name,len(body),sha,fingerprint(meta),None))
        db.execute("UPDATE entries SET meta=CAST(x'ff' AS TEXT) WHERE job='history-source' AND path='disk:/file-0000.bin'")
        settings=dict(settings,operation='missing',_inherit_source={'job':'history-source'})
    if args.portable:
        from disk_downloader.import_state import save_import
        with store.db() as db:
            db.execute("UPDATE entries SET meta=? WHERE job='history-source' AND path='disk:/file-0000.bin'",(json.dumps({'path':'disk:/file-0000.bin','type':'file','size':len(body),'sha256':sha}),))
        save_import(store,'history-source')
        manifest=store.job('history-source')['settings']['manifest_local']
        with closing(sqlite3.connect(root/manifest)) as db,db:
            db.execute("UPDATE inventory SET meta=CAST(x'ff' AS TEXT) WHERE path='disk:/file-0000.bin'")
        settings.update(_inherit_source={'manifest':manifest},manifest_local=manifest)
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES(?,?,?,?,?,?,?)',('history-target',json.dumps(settings),str(root),'__service','interrupted','2026-01-02',0))
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    env={key:value for key,value in os.environ.items() if not key.startswith('PYTHON')}
    env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
    process=subprocess.Popen([str(exe),'--no-browser','--port',str(port),'--state-dir',str(state)],cwd=folder,env=env)
    with requests.Session() as session:
        session.trust_env=False;started=time.monotonic();headers={}
        try:
            due=time.monotonic()+30
            while time.monotonic()<due:
                assert process.poll() is None,'EXE failed at startup'
                try:
                    response=session.get(url+'/',timeout=1)
                    if response.status_code==200:break
                except requests.RequestException:pass
                time.sleep(.1)
            else:raise AssertionError('EXE startup timed out')
            headers={'Origin':url,'X-CSRF-Token':re.search(r'name="csrf-token" content="([^"]+)"',response.text).group(1)}
            response=session.post(url+'/api/jobs/history-target/action',json={'action':'save_manifest'},headers=headers,timeout=5);response.raise_for_status()
            samples=[];due=time.monotonic()+60
            while time.monotonic()<due:
                response=session.get(url+'/api/jobs/history-target',headers=headers,timeout=5);response.raise_for_status();job=response.json()
                samples.extend(item for item in job.get('active',[]) if item.get('unit')=='local_files' and item.get('size') is not None)
                if not job['live']:break
                time.sleep(.03)
            else:raise AssertionError('History import did not finish')
            if args.old:
                assert job['phase']=='failed' and 'SQLITE_ERROR' in job['error']
                diagnostic=session.get(url+'/api/jobs/history-target/diagnostics-preview',headers=headers,timeout=5).json()['files']['requests.json']
                assert len(diagnostic)==1 and diagnostic[0]['sqlite_code']==''
                assert diagnostic[0]['sqlite']['reason']=='unknown'
                assert [frame['function'] for frame in diagnostic[0]['frames']][-4:]==['_run','inherit','copy','records']
            else:
                assert job['phase']=='stopped' and not job['settings'].get('_inherit_source'),job.get('error')
                assert job['settings']['history_warning']
                assert (root/job['settings']['manifest_local']).is_file()
                with store.db() as db:
                    assert db.execute("SELECT COUNT(*) FROM artifacts WHERE job='history-target'").fetchone()[0]==count
                    assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                if args.portable:
                    backup=root/job['settings']['history_backup_local']
                    with closing(sqlite3.connect(backup)) as db:
                        assert db.execute("SELECT CAST(meta AS BLOB) FROM inventory WHERE path='disk:/file-0000.bin'").fetchone()[0]==b'\xff'
                else:
                    with store.db() as db:
                        assert db.execute("SELECT CAST(meta AS BLOB) FROM entries WHERE job='history-source' AND path='disk:/file-0000.bin'").fetchone()[0]==b'\xff'
            assert all(hashlib.sha256((root/f'file-{index:04}.bin').read_bytes()).hexdigest()==sha for index in range(count))
            result={'actual_exe':True,'source':'portable' if args.portable else 'application','before_fix':args.old,'phase':job['phase'],'files_preserved':count,'sha256_ok':True,'history_preserved':True,'seconds':round(time.monotonic()-started,3)}
            session.post(url+'/api/exit',json={},headers=headers,timeout=5).raise_for_status();process.wait(timeout=20);assert process.returncode==0
            args.output.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
        finally:
            if process.poll() is None:
                if headers:
                    try:session.post(url+'/api/exit',json={},headers=headers,timeout=5);process.wait(timeout=20)
                    except (requests.RequestException,subprocess.TimeoutExpired):pass
                if process.poll() is None:subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True);process.wait(timeout=10)
