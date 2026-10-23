import hashlib
import json
import os
import re
import shutil
import stat
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
from .paths import native


def _native_path(path):
    path = Path(path)
    # Preserve ordinary launch/config spelling; descendants near MAX_PATH use
    # explicit native Windows paths without changing machine-wide policy.
    if os.name == 'nt' and len(str(path.absolute()).encode('utf-16-le')) >= 480:
        return Path(native(path))
    return path


STAGE_FORMAT = 'YaDiskDownloader-update-stage-v1'
STAGE_MANIFEST = '.update-stage.json'
STAGE_LEASE = '.update-lease'
STAGE_RETAIN = 2
STAGE_SCAN_LIMIT = 256
STAGE_FILE_LIMIT = 10
STAGE_PRUNE_GENERATIONS = 2
STAGE_PRUNE_BYTES = 2 * 1024**3
STAGE_PRUNE_SECONDS = 2


def _stage_plain(path, directory=False):
    info = _native_path(path).lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1


class _StageLease:
    """The open OS lock, rather than a departed parent PID, proves liveness."""
    def __init__(self, stage, create=False, timeout=0):
        self.stage = Path(native(stage))
        filename = self.stage / STAGE_LEASE
        flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(filename, flags, 0o600)
        self.stream = os.fdopen(descriptor, 'r+b', buffering=0)
        self.locked = False
        self.marker_sha = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Update lease is not an ordinary file')
            if create:
                self.stream.write(uuid.uuid4().hex.encode('ascii'))
                self.stream.flush(); os.fsync(descriptor)
            if os.fstat(descriptor).st_size != 32:
                raise ValueError('Invalid update lease')
            due = time.monotonic() + timeout
            while True:
                try:
                    self.stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 32)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.locked = True
                    self.stream.seek(0)
                    nonce = self.stream.read(32)
                    self.record = {'size': 32, 'sha256': hashlib.sha256(nonce).hexdigest()}
                    break
                except OSError:
                    if time.monotonic() >= due:
                        raise
                    time.sleep(.05)
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        if not self.stream.closed:
            try:
                if self.locked:
                    self.stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 32)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.locked = False
                self.stream.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _stage_record(filename, expected=None):
    filename = _native_path(filename)
    if not _stage_plain(filename):
        raise ValueError('Update artifact is linked or not an ordinary file')
    size = filename.stat().st_size
    sha = digest_file(filename)
    if expected is not None and sha != expected:
        raise ValueError('Update artifact was modified')
    return {'size': size, 'sha256': sha}


def _stage_metadata(stage, check_content=False, lease=None, checkpoint=lambda: None):
    stage = Path(native(stage))
    if not re.fullmatch('[a-f0-9]{32}', stage.name) or not _stage_plain(stage, True):
        return None
    marker = stage / STAGE_MANIFEST
    if not _stage_plain(marker) or marker.stat().st_size > 64 * 1024:
        return None
    raw = marker.read_bytes()
    try:
        meta = json.loads(raw)
    except (ValueError,TypeError,RecursionError):
        return None
    if not isinstance(meta, dict) or set(meta) != {'format','id','archive','created_ns','state','files'} or meta.get('format') != STAGE_FORMAT or meta.get('id') != stage.name:
        return None
    if meta.get('state') not in ('downloading', 'ready', 'download_failed', 'installing', 'success', 'recovery'):
        return None
    if type(meta.get('created_ns')) is not int or meta['created_ns'] <= 0:
        return None
    archive = meta.get('archive')
    if not isinstance(archive, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,180}\.zip', archive):
        return None
    allowed = {archive, 'new.exe', 'launcher.exe', 'install.json', 'previous.exe', 'ready.json', 'result.json', STAGE_LEASE}
    files = meta.get('files')
    if not isinstance(files, dict) or not files or len(files) > STAGE_FILE_LIMIT or not set(files) <= allowed or STAGE_LEASE not in files:
        return None
    for name, record in files.items():
        if not isinstance(record, dict) or set(record) != {'size','sha256'} or type(record.get('size')) is not int or not 0 <= record['size'] <= 1024**3 or not isinstance(record.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', record['sha256']):
            return None
    # Missing inventoried files are allowed for retrying a partially completed
    # cleanup. Every remaining file must still be known and verified.
    count = 0
    for child in stage.iterdir():
        checkpoint()
        count += 1
        if count > STAGE_FILE_LIMIT + 1 or child.name not in set(files) | {STAGE_MANIFEST} or not _stage_plain(child):
            return None
        if child.name in files and (child.stat().st_size != files[child.name]['size'] or check_content and (lease.record['sha256'] if child.name == STAGE_LEASE and lease is not None else digest_file(child, checkpoint)) != files[child.name]['sha256']):
            return None
    return meta, hashlib.sha256(raw).hexdigest()


def _write_stage(lease, meta):
    """Only this lease's unchanged marker may be replaced."""
    stage = lease.stage
    marker = stage / STAGE_MANIFEST
    if marker.exists():
        if lease.marker_sha is None or _stage_record(marker)['sha256'] != lease.marker_sha:
            raise ValueError('Update stage metadata was modified')
    elif lease.marker_sha is not None:
        raise ValueError('Update stage metadata disappeared')
    temporary = stage / ('.stage-write-' + uuid.uuid4().hex)
    try:
        raw = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode('utf-8')
        with temporary.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, marker)
        lease.marker_sha = hashlib.sha256(raw).hexdigest()
    finally:
        if temporary.exists():
            temporary.unlink()


def _prune_stages(folder):
    """Only bounded, understood generations are eligible for deletion."""
    from .preview_cleanup import remove_owned
    candidates = []
    due = time.monotonic() + STAGE_PRUNE_SECONDS
    def checkpoint():
        if time.monotonic() >= due:
            raise TimeoutError('Update cleanup time budget reached')
    try:
        if not _stage_plain(folder, True):
            return
        with os.scandir(folder) as iterator:
            for index, entry in enumerate(iterator):
                if index >= STAGE_SCAN_LIMIT:
                    break
                checkpoint()
                try:
                    data = _stage_metadata(Path(entry.path))
                    if data and data[0]['state'] in ('ready', 'download_failed', 'success'):
                        candidates.append((data[0]['created_ns'], _native_path(entry.path), data))
                except (OSError, ValueError, TypeError):
                    continue
    except OSError:
        return
    candidates.sort(reverse=True, key=lambda value: (value[0], str(value[1])))
    keep = set()
    successes = [path for _, path, data in candidates if data[0]['state'] == 'success']
    if successes:
        keep.add(successes[0])
    # Locked stages count as protected generations, including a prepared update.
    leases = []
    try:
        for _, stage, data in candidates:
            try:
                lease = _StageLease(stage)
                lease.marker_sha = data[1]
                leases.append((stage, data, lease))
            except (OSError, ValueError):
                keep.add(stage)
        for _, stage, _ in candidates:
            if len(keep) >= STAGE_RETAIN:
                break
            keep.add(stage)
        generations = bytes_checked = 0
        for stage, original, lease in leases:
            if stage in keep:
                continue
            size = sum(record['size'] for record in original[0]['files'].values())
            if generations >= STAGE_PRUNE_GENERATIONS or bytes_checked + size > STAGE_PRUNE_BYTES:
                break
            generations += 1; bytes_checked += size
            try:
                checkpoint()
                data = _stage_metadata(stage, True, lease, checkpoint)
                if not data or data[1] != original[1]:
                    continue
                meta, marker_sha = data
                complete = True
                for name, record in meta['files'].items():
                    if name == STAGE_LEASE:
                        continue
                    if not remove_owned(stage, {'local': name, **record}, checkpoint):
                        complete = False; break
                if not complete:
                    continue
                marker = stage / STAGE_MANIFEST
                if not remove_owned(stage, {'local': STAGE_MANIFEST, 'size': marker.stat().st_size, 'sha256': marker_sha}, checkpoint):
                    continue
                lease.close()
                if remove_owned(stage, {'local': STAGE_LEASE, **meta['files'][STAGE_LEASE]}, checkpoint):
                    stage.rmdir()
            except (OSError, ValueError, TypeError):
                continue
    finally:
        for _, _, lease in leases:
            lease.close()


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
    archive, candidate = _native_path(archive), _native_path(candidate)
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
        self.folder=Path(native(Path(engine.store.filename).parent/'updates'))
        self.lock=threading.RLock(); self.result={'state':'idle','bytes':0,'message':''}
        self.prepared=None; self._lease=None

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
        stage = lease = meta = None
        archive = candidate = None
        if self._lease is not None:
            self._lease.close(); self._lease = None
        with self.lock:
            self.prepared = None
        try:
            self.folder.mkdir(exist_ok=True)
            if not _stage_plain(self.folder, True):
                raise ValueError('Папка обновлений является ссылкой')
            _prune_stages(self.folder)
            if not isinstance(asset.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,180}\.zip', asset['name']):
                raise ValueError('Некорректное имя архива обновления')
            stage=_native_path(self.folder/uuid.uuid4().hex); stage.mkdir()
            lease=_StageLease(stage,create=True)
            meta={'format':STAGE_FORMAT,'id':stage.name,'archive':asset['name'],
                  'created_ns':time.time_ns(),'state':'downloading','files':{STAGE_LEASE:lease.record}}
            _write_stage(lease,meta)
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
            candidate_sha=digest_file(candidate)
            meta['files'].update({asset['name']:{'size':count,'sha256':sha.hexdigest()},
                                  'new.exe':_stage_record(candidate,candidate_sha)})
            meta['state']='ready'; _write_stage(lease,meta)
            with self.lock:
                self.prepared={'stage':str(stage),'candidate':str(candidate),'sha256':candidate_sha,'version':release['latest'].lstrip('v'),'repository':release['repository']}
                self._lease=lease; lease=None
                self.result.update(state='ready',message='Версия '+self.prepared['version']+' из '+release['repository']+' проверена. Установите после завершения работы.',archive=str(archive))
        except Exception:
            if lease is not None and meta is not None:
                try:
                    for artifact in (archive,candidate):
                        if artifact is not None and artifact.exists():
                            meta['files'][artifact.name]=_stage_record(artifact)
                    meta['state']='download_failed'; _write_stage(lease,meta)
                except (OSError,ValueError):
                    pass  # An unsealed or changed stage must be preserved.
            with self.lock: self.result.update(state='error',message='Не удалось скачать или проверить обновление. Установленная программа сохранена.')
        finally:
            if lease is not None:
                lease.close()
            _prune_stages(self.folder)

    def install(self, quit_app):
        with self.engine.lock, self.lock:
            self.engine.ensure_idle()
            if not frozen() or os.name!='nt' or not quit_app: raise ValueError('Автоматическая установка доступна в Windows EXE')
            if self.result['state']!='ready' or not self.prepared: raise ValueError('Сначала скачайте и проверьте обновление')
            if self.prepared['repository']!=self.updates.settings()['repository']:
                raise ValueError('Репозиторий изменён. Скачайте обновление из выбранного источника заново')
            stage=_native_path(self.prepared['stage']); candidate=_native_path(self.prepared['candidate'])
            target=_native_path(sys.executable).resolve()
            if target.name!='YaDiskDownloader.exe':
                raise ValueError('Обновление программы на ПК не начато: файл приложения переименован. Закройте программу, верните имя YaDiskDownloader.exe и запустите её снова. Текущая версия сохранена.')
            if candidate.resolve().parent!=stage.resolve() or not re.fullmatch('[a-f0-9]{32}',stage.name) or stage.parent.name!='updates':
                raise ValueError('Папка подготовленного обновления изменилась. Скачайте обновление заново; текущая программа продолжает работать.')
            managed = _stage_metadata(stage, False, self._lease) if (stage/STAGE_MANIFEST).exists() else None
            if (stage/STAGE_MANIFEST).exists() and (managed is None or self._lease is None or managed[1]!=self._lease.marker_sha):
                raise ValueError('Файлы подготовленного обновления изменились. Скачайте обновление заново; текущая программа продолжает работать.')
            if digest_file(candidate)!=self.prepared['sha256']: raise ValueError('Подготовленное обновление изменено')
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
            if managed is not None:
                meta=managed[0]
                meta['files']['launcher.exe']=_stage_record(launcher,config['old_sha256'])
                meta['files']['install.json']=_stage_record(config_path)
                meta['state']='installing'; _write_stage(self._lease,meta)
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


def _copy_update_file(source, target, expected):
    source, target = _native_path(source), _native_path(target)
    # A fresh stage must never overwrite an unrelated pre-existing file.
    if os.path.lexists(target):
        raise FileExistsError('Update copy destination already exists')
    shutil.copyfile(source,target)
    with open(target,'r+b') as stream:
        stream.flush(); os.fsync(stream.fileno())
    if digest_file(target)!=expected:
        raise ValueError('Copied update file failed verification')


def _replace_update_file(source, target):
    source, target = _native_path(source), _native_path(target)
    for attempt in range(30):
        try:
            os.replace(source,target)
            return
        except PermissionError:
            if attempt==29:
                raise
            time.sleep(.5)


def _update_result(stage, value):
    # A full/unavailable drive must not prevent restarting the working EXE.
    try:
        (_native_path(stage)/'result.json').write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    except OSError:
        pass


def _restore_update_database(config):
    import sqlite3
    from contextlib import closing
    from .backups import backup_connection, readonly
    source_file=Path(config['state_dir'])/'backups'/config['backup']
    with closing(readonly(source_file)) as source, closing(sqlite3.connect(_native_path(Path(config['state_dir'])/'jobs.sqlite3'))) as database:
        database.execute('PRAGMA synchronous=FULL')
        backup_connection(source,database)
        database.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def _apply_update(config_path, prepare=lambda: None):
    """Runs from a copy of the current EXE, never from downloaded helper code."""
    file=_native_path(config_path).resolve(); stage=file.parent
    config=json.loads(file.read_text(encoding='utf-8'))
    target=_native_path(config['target']); candidate=_native_path(config['candidate'])
    if target.name!='YaDiskDownloader.exe' or candidate.resolve().parent!=stage or not re.fullmatch('[a-f0-9]{32}',stage.name) or stage.parent.name!='updates':
        raise ValueError('Некорректные пути обновления')
    if (not target.is_absolute() or not Path(config['state_dir']).is_absolute()
            or not isinstance(config['backup'],str) or Path(config['backup']).name!=config['backup']
            or not re.fullmatch('[a-f0-9]{64}',config['sha256'])
            or not re.fullmatch('[a-f0-9]{64}',config['old_sha256'])):
        raise ValueError('Некорректные параметры восстановления обновления')
    backup=stage/'previous.exe'
    replacement=target.parent/('.update-'+stage.name+'.exe')
    base_args=['--port',str(config['port']),'--state-dir',config['state_dir']]
    if config.get('no_browser'): base_args.append('--no-browser')
    if config.get('no_tray'): base_args.append('--no-tray')
    ack=stage/'ready.json'
    installed=False; process=None; old_exited=False; step='verify_original'
    try:
        prepare()
        if digest_file(target)!=config['old_sha256']:
            raise ValueError('Previous executable changed')
        step='wait_previous'
        if not wait_process(config['pid'],60):
            raise ValueError('Previous process did not exit')
        old_exited=True
        step='verify_candidate'
        if digest_file(candidate)!=config['sha256']:
            raise ValueError('Update candidate changed')
        step='backup_executable'
        _copy_update_file(target,backup,config['old_sha256'])
        step='prepare_replacement'
        _copy_update_file(candidate,replacement,config['sha256'])
        # Recheck the old generation immediately before replacing its name.
        if digest_file(target)!=config['old_sha256']:
            raise ValueError('Previous executable changed during preparation')
        step='replace_executable'
        _replace_update_file(replacement,target)
        installed=True
        step='start_new'
        process=subprocess.Popen([str(target),*base_args,'--update-ack',str(ack)],creationflags=subprocess.CREATE_NO_WINDOW)
        step='confirm_startup'
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
                        _update_result(stage,{'ok':True,'version':config['version']})
                        return
                    except requests.RequestException:
                        pass
            if process.poll() is not None: break
            time.sleep(.2)
        raise ValueError('Новая версия не подтвердила запуск')
    except Exception as error:
        failure_step=step
        if not old_exited and failure_step=='verify_original':
            # Frozen helper startup can outlast the old app's shutdown timer.
            # A transient first read failure must not skip a safe relaunch.
            try:
                old_exited=wait_process(config['pid'],60)
            except Exception:
                pass
        stopped=True
        if process and process.poll() is None:
            try:
                subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=10)
                stopped=process.poll() is not None
            except Exception:
                stopped=False
        restored=False; database_ready=not installed; restart=None; restarted=False
        if old_exited and stopped:
            try:
                restored=digest_file(target)==config['old_sha256']
            except OSError:
                pass
            if installed and not restored:
                try:
                    # Never overwrite a file substituted after our replacement.
                    if digest_file(target)!=config['sha256']:
                        raise ValueError('Installed executable was replaced')
                    _copy_update_file(backup,replacement,config['old_sha256'])
                    _replace_update_file(replacement,target)
                    restored=True
                except Exception:
                    pass
            if installed:
                try:
                    _restore_update_database(config)
                    database_ready=True
                except Exception:
                    database_ready=False
            if database_ready:
                for executable in ((target,backup) if installed else (target,)):
                    try:
                        if digest_file(executable)!=config['old_sha256']:
                            continue
                        subprocess.Popen([str(executable),*base_args],creationflags=subprocess.CREATE_NO_WINDOW)
                        restart=executable
                        restarted=True
                        break
                    except Exception:
                        pass
        message='Обновление программы на ПК не установлено. '
        if restarted:
            message+='Предыдущая версия запущена снова.' if restored else 'Предыдущая версия запущена из сохранённой копии; основной EXE не удалось восстановить.'
        elif not old_exited:
            message+='Рабочий процесс не подтверждён как завершённый; повторный экземпляр не запускался.'
        elif not stopped:
            message+='Новая версия ещё работает: завершить её не удалось. Прежняя копия сохранена; повторный экземпляр не запускался.'
        elif not database_ready:
            message+='Не удалось восстановить историю. Сохранённую копию EXE и резервную копию истории не удаляйте; требуется ручное восстановление.'
        else:
            message+='Автоматический запуск прежней версии не удался. Сохранённые копии не удаляйте; запустите программу вручную.'
        _update_result(stage,{'ok':False,'stage':failure_step,'error_type':type(error).__name__,
                              'os_code':getattr(error,'winerror',None) or getattr(error,'errno',None),
                              'message':message,'restored':restored,'restarted':restarted,
                              'recovery_executable':str(restart) if restart else None})


def apply_update(config_path):
    """A managed helper owns its lease through startup and rollback."""
    file=_native_path(config_path).resolve(); stage=file.parent
    lease=data=None
    try:
        if (stage/STAGE_MANIFEST).exists():
            # A busy lease can belong to an updater, not merely the old app.
            # Never execute a second helper or race that updater's rollback.
            lease=_StageLease(stage,timeout=60)
        def prepare():
            nonlocal data
            if lease is not None:
                data=_stage_metadata(stage,False,lease)
                if data is None or data[0]['state']!='installing':
                    raise ValueError('Подготовленные файлы обновления на ПК изменились; сохранённые копии не удалены.')
                lease.marker_sha=data[1]
        # Metadata read errors belong inside the ordinary pre-install recovery
        # guard: the previous process may already have exited by this point.
        _apply_update(file,prepare)
        if lease is not None and data is not None:
            try:
                meta=data[0]
                result=json.loads((stage/'result.json').read_text(encoding='utf-8'))
                config=json.loads(file.read_text(encoding='utf-8'))
                for name in ('previous.exe','ready.json','result.json'):
                    artifact=stage/name
                    if artifact.exists():
                        meta['files'][name]=_stage_record(artifact,config['old_sha256'] if name=='previous.exe' else None)
                meta['state']='success' if result.get('ok') is True else 'recovery'
                _write_stage(lease,meta)
            except (OSError,ValueError,TypeError):
                pass  # Installing/recovery stages remain protected if sealing fails.
    finally:
        if lease is not None:
            lease.close()
