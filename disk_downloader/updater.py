import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urljoin

import requests
from .api import Deadline, DeadlineAdapter
from .runtime import frozen
from .service_outputs import digest_file
from .updates import version


def download_url(url, repo, first=False):
    if not isinstance(url,str): raise ValueError('Некорректный адрес обновления')
    value=urlsplit(url)
    if value.scheme!='https' or value.port not in (None,443) or value.username or value.password or value.fragment:
        raise ValueError('Недопустимый адрес обновления')
    if first:
        if value.hostname!='github.com' or not value.path.startswith('/'+repo+'/releases/download/') or value.query:
            raise ValueError('Архив должен принадлежать выбранному репозиторию GitHub')
    elif value.hostname not in ('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):
        raise ValueError('Недопустимое перенаправление обновления')
    return url


def validate_archive(archive, candidate):
    with zipfile.ZipFile(archive) as source:
        entries=source.infolist()
        if len(entries)>300 or sum(e.file_size for e in entries)>1024*1024*1024:
            raise ValueError('Архив обновления слишком большой')
        executables=[]
        for entry in entries:
            path=PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename or entry.flag_bits&1 or (entry.external_attr>>16)&0o170000==0o120000:
                raise ValueError('Небезопасный путь в архиве обновления')
            if path.name=='YaDiskDownloader.exe': executables.append(entry)
        if len(executables)!=1 or not 1024<=executables[0].file_size<=512*1024*1024:
            raise ValueError('В архиве нет единственной Windows-программы')
        with source.open(executables[0]) as input, open(candidate,'xb') as output:
            if input.read(2)!=b'MZ': raise ValueError('Некорректный EXE в архиве')
            output.write(b'MZ')
            shutil.copyfileobj(input,output,1024*1024)
            output.flush(); os.fsync(output.fileno())


class Installer:
    def __init__(self,engine,updates,backups,port):
        self.engine=engine; self.updates=updates; self.backups=backups; self.port=port
        self.folder=Path(engine.store.filename).parent/'updates'
        self.lock=threading.RLock(); self.result={'state':'idle','bytes':0,'message':''}
        self.prepared=None

    def snapshot(self):
        with self.lock: return dict(self.result,can_install=frozen() and os.name=='nt')

    def download(self):
        with self.lock:
            if self.result['state'] in ('downloading','installing'):
                raise ValueError('Обновление уже выполняется')
            release=self.updates.snapshot()
            asset=release.get('asset')
            if not release.get('available') or not asset:
                raise ValueError('Для нового релиза нужен Windows-архив с контрольной суммой GitHub')
            if not isinstance(asset.get('size'),int) or not 0<asset['size']<=200*1024*1024:
                raise ValueError('Некорректный размер обновления')
            download_url(asset['browser_download_url'],release['repository'],True)
            self.prepared=None; self.result={'state':'downloading','bytes':0,'total':asset['size'],'message':'Скачивание обновления'}
            try:
                threading.Thread(target=self.fetch,args=(release,asset),name='disk-update-download',daemon=True).start()
            except RuntimeError:
                self.result.update(state='error',message='На ПК не удалось начать скачивание обновления. Закройте лишние программы и повторите.')
                raise ValueError(self.result['message']) from None
        return self.snapshot()

    def fetch(self, release, asset):
        try:
            self.folder.mkdir(exist_ok=True)
            if self.folder.is_symlink() or (hasattr(self.folder,'is_junction') and self.folder.is_junction()):
                raise ValueError('Папка обновлений является ссылкой')
            stage=self.folder/uuid.uuid4().hex; stage.mkdir()
            archive=stage/asset['name']; sha=hashlib.sha256(); count=0
            with Deadline(300) as deadline, requests.Session() as session:
                session.trust_env=False; session.mount('https://',DeadlineAdapter(deadline))
                url=asset['browser_download_url']
                for redirect in range(5):
                    download_url(url,release['repository'],redirect==0)
                    response=session.get(url,timeout=(5,20),allow_redirects=False,stream=True)
                    deadline.attach(response=response)
                    if response.status_code in (301,302,303,307,308):
                        next_url=urljoin(url,response.headers.get('Location',''))
                        response.close(); url=next_url; continue
                    if response.status_code!=200:
                        response.close(); raise ValueError('GitHub не отдал архив обновления')
                    with response, open(archive,'xb') as stream:
                        for chunk in response.iter_content(1024*1024):
                            deadline.check(); count+=len(chunk)
                            if count>asset['size']: raise ValueError('Размер обновления не совпадает')
                            sha.update(chunk); stream.write(chunk)
                            with self.lock: self.result['bytes']=count
                        stream.flush(); os.fsync(stream.fileno())
                    break
                else: raise ValueError('Слишком много перенаправлений обновления')
            if count!=asset['size'] or sha.hexdigest()!=asset['digest'].split(':')[1]:
                raise ValueError('Обновление не прошло проверку SHA-256')
            candidate=stage/'new.exe'; validate_archive(archive,candidate)
            with self.lock:
                self.prepared={'stage':str(stage),'candidate':str(candidate),'sha256':digest_file(candidate),'version':release['latest'].lstrip('v'),'repository':release['repository']}
                self.result.update(state='ready',message='Версия '+self.prepared['version']+' из '+release['repository']+' проверена. Установите после завершения работы.',archive=str(archive))
        except Exception:
            with self.lock: self.result.update(state='error',message='Не удалось скачать или проверить обновление. Установленная программа сохранена.')

    def install(self, quit_app):
        with self.engine.lock, self.lock:
            self.engine.ensure_idle()
            if not frozen() or os.name!='nt' or not quit_app: raise ValueError('Автоматическая установка доступна в Windows EXE')
            if self.result['state']!='ready' or not self.prepared: raise ValueError('Сначала скачайте и проверьте обновление')
            if self.prepared['repository']!=self.updates.settings()['repository']:
                raise ValueError('Репозиторий изменён. Скачайте обновление из выбранного источника заново')
            stage=Path(self.prepared['stage']); candidate=Path(self.prepared['candidate'])
            if digest_file(candidate)!=self.prepared['sha256']: raise ValueError('Подготовленное обновление изменено')
            target=Path(sys.executable).resolve()
            # Check directory writability before ending the app.
            probe=target.parent/('.update-probe-'+uuid.uuid4().hex)
            with probe.open('xb') as stream: stream.write(b'probe')
            probe.unlink()
            backup=self.backups.create()
            with self.engine.store.db() as db:
                db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','true')")
            launcher=stage/'launcher.exe'; shutil.copyfile(target,launcher)
            config=self.prepared|{'target':str(target),'old_sha256':digest_file(target),'state_dir':str(Path(self.engine.store.filename).parent.resolve()),'port':self.port,'pid':os.getpid(),'backup':backup}
            config_path=stage/'install.json'
            with config_path.open('x',encoding='utf-8') as stream:
                json.dump(config,stream); stream.flush(); os.fsync(stream.fileno())
            subprocess.Popen([str(launcher),'--apply-update',str(config_path)],creationflags=subprocess.CREATE_NO_WINDOW)
            self.engine.shutting_down=True
            self.result.update(state='installing',message='Перезапуск программы для установки обновления')
            threading.Timer(.5,quit_app).start()
        return self.snapshot()


def wait_process(pid, timeout):
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes=(ctypes.c_void_p,ctypes.c_uint32)
    kernel.CloseHandle.argtypes=(ctypes.c_void_p,)
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle: return True
    try: return kernel.WaitForSingleObject(handle,int(timeout*1000))==0
    finally: kernel.CloseHandle(handle)


def apply_update(config_path):
    """Runs from a copy of the current EXE, never from downloaded helper code."""
    file=Path(config_path).resolve(); stage=file.parent
    config=json.loads(file.read_text(encoding='utf-8'))
    target=Path(config['target']); candidate=Path(config['candidate'])
    if target.name!='YaDiskDownloader.exe' or candidate.resolve().parent!=stage or not re.fullmatch('[a-f0-9]{32}',stage.name) or stage.parent.name!='updates':
        raise ValueError('Некорректные пути обновления')
    if digest_file(candidate)!=config['sha256'] or digest_file(target)!=config['old_sha256']:
        raise ValueError('Файлы обновления изменены')
    if not wait_process(config['pid'],60): raise ValueError('Программа не завершилась')
    backup=stage/'previous.exe'; shutil.copyfile(target,backup)
    replacement=target.parent/('.update-'+stage.name+'.exe')
    shutil.copyfile(candidate,replacement)
    base_args=['--port',str(config['port']),'--state-dir',config['state_dir']]
    if config.get('no_browser'): base_args.append('--no-browser')
    if config.get('no_tray'): base_args.append('--no-tray')
    ack=stage/'ready.json'
    installed=False; process=None
    try:
        for attempt in range(30):
            try: os.replace(replacement,target); installed=True; break
            except PermissionError: time.sleep(.5)
        if not installed: raise ValueError('EXE занят другим процессом')
        process=subprocess.Popen([str(target),*base_args,'--update-ack',str(ack)],creationflags=subprocess.CREATE_NO_WINDOW)
        due=time.monotonic()+min(60,max(5,float(config.get('startup_timeout',40))))
        while time.monotonic()<due:
            if ack.is_file():
                ready=json.loads(ack.read_text(encoding='utf-8'))
                if ready.get('version')==config['version']:
                    try:
                        with requests.Session() as session:
                            session.trust_env=False
                            live=session.get('http://127.0.0.1:'+str(config['port'])+'/',timeout=1)
                            live.raise_for_status()
                        (stage/'result.json').write_text(json.dumps({'ok':True,'version':config['version']}),encoding='utf-8')
                        return
                    except requests.RequestException:
                        pass
            if process.poll() is not None: break
            time.sleep(.2)
        raise ValueError('Новая версия не подтвердила запуск')
    except Exception:
        if process and process.poll() is None:
            subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            process.wait(timeout=10)
        if installed:
            shutil.copyfile(backup,replacement)
            for attempt in range(30):
                try: os.replace(replacement,target); break
                except PermissionError:
                    if attempt==29: raise
                    time.sleep(.5)
            # Restore the consistent pre-update database before relaunching the old EXE.
            import sqlite3
            from contextlib import closing
            from .backups import readonly
            source_file=Path(config['state_dir'])/'backups'/config['backup']
            with closing(readonly(source_file)) as source, closing(sqlite3.connect(Path(config['state_dir'])/'jobs.sqlite3')) as database:
                database.execute('PRAGMA synchronous=FULL')
                source.backup(database)
                database.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        (stage/'result.json').write_text(json.dumps({'ok':False,'message':'Обновление отменено; восстановлена предыдущая программа'}),encoding='utf-8')
        subprocess.Popen([str(target),*base_args],creationflags=subprocess.CREATE_NO_WINDOW)
