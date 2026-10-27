"""Real EXE recovery prompts and data preservation using synthetic corrupt indexes."""
import argparse,hashlib,json,os,socket,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path
import requests
import win32gui,win32process
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(ROOT/'tools')]
from test_history_integrity_recovery import corrupt,history,large_history
from smoke_windows_exe import children

parser=argparse.ArgumentParser();parser.add_argument('--exe',required=True,type=Path);parser.add_argument('--output',required=True,type=Path);parser.add_argument('--large',action='store_true');args=parser.parse_args()
exe=args.exe.resolve(strict=True);results=[]
for mode in (('indexes_yes',) if args.large else ('indexes_yes','indexes_no','backup_yes')):
 with tempfile.TemporaryDirectory(prefix='yadisk-history-recovery-exe-') as directory:
  root=Path(directory);state=root/'AppData/YaDiskDownloader';state.mkdir(parents=True);database=state/'jobs.sqlite3'
  if args.large:
   import shutil
   assert shutil.disk_usage(root).free>9*1024**3,'Need 9 GiB free for the large history probe'
   large_history(database)
  else:corrupt(database)
  history_bytes=database.stat().st_size
  photo=root/'kept-photo.bin';photo.write_bytes(b'synthetic previously downloaded data')
  with closing(sqlite3.connect(database)) as db,db:
   db.execute("INSERT INTO preferences VALUES('updates',?)",(json.dumps({'repository':'murzirius/YaDiskDownloader','automatic':False}),))
   if mode=='backup_yes':
    db.execute('CREATE TABLE bad_check(value INTEGER CHECK(value>0))')
    db.execute('PRAGMA ignore_check_constraints=ON');db.execute('INSERT INTO bad_check VALUES(-1)')
  before=hashlib.sha256(database.read_bytes()).hexdigest()
  if mode=='backup_yes':
   (state/'backups').mkdir();history(state/'backups/before-1.5.0.sqlite3',rows=9)
  with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
  env={k:v for k,v in os.environ.items() if not k.startswith('PYTHON')};env['LOCALAPPDATA']=str(root/'AppData')
  process=subprocess.Popen([str(exe),'--port',str(port)],cwd=root,env=env)
  session=requests.Session();session.trust_env=False;url=f'http://127.0.0.1:{port}';pids=[];dialog=None;windows=[];response=None
  try:
   deadline=time.monotonic()+(180 if args.large else 30)
   while time.monotonic()<deadline:
    assert process.poll() is None,'EXE exited before a recovery choice'
    if not pids:pids=children(process.pid)
    found=[]
    def scan(hwnd,_):
     if win32process.GetWindowThreadProcessId(hwnd)[1] in pids:found.append(hwnd)
    win32gui.EnumWindows(scan,None)
    matches=[w for w in found if win32gui.GetWindowText(w)=='Восстановление истории YaDiskDownloader']
    if matches:dialog=matches[0];windows=[w for w in found if win32gui.GetClassName(w)=='YaDiskDownloaderDesktop'];break
    time.sleep(.1)
   assert dialog is not None and len(windows)==1,'Recovery confirmation missing'
   texts=[];win32gui.EnumChildWindows(dialog,lambda h,_:texts.append(win32gui.GetWindowText(h)),None)
   message=' '.join(texts)
   assert hashlib.sha256(database.read_bytes()).hexdigest()==before,'Original changed before consent'
   assert 'recovery-originals' in message
   if mode=='backup_yes':assert 'before-1.5.0.sqlite3' in message and 'после этой даты' in message
   else:assert 'индексы' in message and 'Полная проверка SQLite пройдена' in message
   decision=7 if mode=='indexes_no' else 6
   win32gui.PostMessage(dialog,0x0111,decision,win32gui.GetDlgItem(dialog,decision))
   deadline=time.monotonic()+(180 if args.large else 30)
   observed=set();applied_at=time.monotonic()
   if decision==6:
    while time.monotonic()<deadline:
     assert process.poll() is None
     if args.large:
      win32gui.SendMessageTimeout(windows[0],0,0,0,2,1000)
      text=win32gui.GetWindowText(win32gui.GetDlgItem(windows[0],2))
      if 'Сохраняем исходную историю' in text and 'МБ (' in text:observed.add('save_original')
      if 'Применяем восстановленную историю' in text and 'МБ (' in text:observed.add('apply_recovery')
     try:
      response=session.get(url,timeout=.3)
      if response.status_code==200:break
     except requests.RequestException:pass
     time.sleep(.1)
    assert response is not None and response.status_code==200,'Confirmed recovery did not start application'
    ready_seconds=time.monotonic()-applied_at
    with closing(sqlite3.connect(database)) as db:
     assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
     assert db.execute('SELECT COUNT(*) FROM fixture').fetchone()[0]==(9 if mode=='backup_yes' else 64)
     if args.large:assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==220000
    if args.large:assert observed=={'save_original','apply_recovery'},observed
    originals=list((state/'backups/recovery-originals').glob('original-*.sqlite3'));assert len(originals)==1
    with closing(sqlite3.connect(originals[0])) as db:
     assert db.execute('SELECT COUNT(*) FROM fixture NOT INDEXED').fetchone()[0]==64
   else:
    reports=[]
    while time.monotonic()<deadline:
     reports=list((state/'startup-diagnostics').glob('startup-*.txt'))
     if reports:break
     time.sleep(.1)
    assert reports and json.loads(reports[0].read_text(encoding='utf-8'))['cause']=='history_integrity'
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before
    assert not list((state/'backups/recovery-originals').glob('*.sqlite3'))
   assert photo.read_bytes()==b'synthetic previously downloaded data'
   win32gui.PostMessage(windows[0],0x0010,0,0);process.wait(timeout=25)
   assert process.returncode==(1 if decision==7 else 0)
   results.append({'mode':mode,'actual_exe':True,'native_confirmation':True,'source_unchanged_before_consent':True,'kept_photo_preserved':True,'history_rows_preserved_in_safety_copy':True,'history_bytes':history_bytes,'large':args.large,'copy_progress_observed':sorted(observed),'seconds_until_ready':round(ready_seconds,2) if decision==6 else None,'exit_code':process.returncode})
  finally:
   session.close()
   if process.poll() is None:
    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True);process.wait(timeout=10)
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(results))
