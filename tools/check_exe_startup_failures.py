"""Exercise normal startup and visible diagnostic failures in the real Windows EXE."""
import argparse,hashlib,json,os,re,socket,subprocess,sys,tempfile,time
from pathlib import Path
import requests
import win32gui,win32process
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from smoke_windows_exe import children
from disk_downloader import __build__

parser=argparse.ArgumentParser();parser.add_argument('--exe',required=True,type=Path);parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
exe=args.exe.resolve(strict=True);results=[]
for scenario in ('normal','invalid_history'):
 with tempfile.TemporaryDirectory(prefix='yadisk-startup-check-') as directory:
  root=Path(directory);state=root/'AppData/YaDiskDownloader';state.mkdir(parents=True)
  damaged=state/'jobs.sqlite3'
  if scenario=='invalid_history':damaged.write_bytes(b'synthetic invalid SQLite; preserve this file')
  before=hashlib.sha256(damaged.read_bytes()).hexdigest() if damaged.exists() else None
  with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
  env={k:v for k,v in os.environ.items() if not k.startswith('PYTHON')};env['LOCALAPPDATA']=str(root/'AppData')
  process=subprocess.Popen([str(exe),'--port',str(port)],cwd=root,env=env)
  session=requests.Session();session.trust_env=False;url=f'http://127.0.0.1:{port}';response=None;windows=[];values=[];report=None
  try:
   deadline=time.monotonic()+45
   while time.monotonic()<deadline:
    assert process.poll() is None,'EXE exited before displaying a result'
    if scenario=='normal':
     try:
      response=session.get(url,timeout=.3)
      if response.status_code==200:break
     except requests.RequestException:pass
    else:
     reports=list((state/'startup-diagnostics').glob('startup-*.txt'))
     if reports:report=json.loads(reports[0].read_text(encoding='utf-8'));break
    time.sleep(.1)
   pids=children(process.pid)
   def visit(hwnd,_):
    if win32process.GetWindowThreadProcessId(hwnd)[1] in pids and win32gui.GetClassName(hwnd)=='YaDiskDownloaderDesktop':windows.append(hwnd)
   win32gui.EnumWindows(visit,None);assert len(windows)==1
   deadline=time.monotonic()+5
   while time.monotonic()<deadline:
    values=[];win32gui.EnumChildWindows(windows[0],lambda child,_:values.append((child,win32gui.GetWindowText(child))),None)
    if scenario=='normal' and any(text=='Открыть в браузере' and win32gui.IsWindowEnabled(hwnd) for hwnd,text in values):break
    if scenario=='invalid_history' and any(text=='Открыть диагностику' and win32gui.IsWindowEnabled(hwnd) for hwnd,text in values):break
    time.sleep(.1)
   if scenario=='normal':
    assert response is not None and response.status_code==200
    assert not list((state/'startup-diagnostics').glob('startup-*.txt'))
   else:
    assert report and report['cause']=='database' and report['sqlite_code']=='SQLITE_NOTADB'
    assert report['phase']=='history' and report['build']==__build__
    assert any(text=='Открыть диагностику' and win32gui.IsWindowEnabled(hwnd) for hwnd,text in values)
    assert hashlib.sha256(damaged.read_bytes()).hexdigest()==before
    assert not list((state/'backups').glob('.backup-*'))
    assert str(root) not in json.dumps(report)
   win32gui.PostMessage(windows[0],0x0010,0,0);process.wait(timeout=25)
   assert process.returncode==(0 if scenario=='normal' else 1)
   results.append({'scenario':scenario,'actual_exe':True,'default_startup_path':True,'history_preserved':True,'diagnostics_button':scenario=='invalid_history','exit_code':process.returncode})
  finally:
   session.close()
   if process.poll() is None:
    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True);process.wait(timeout=10)
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(results))
