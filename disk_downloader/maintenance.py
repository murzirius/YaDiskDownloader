import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from .backups import readonly
from .import_state import find_import
from .paths import safe_join, file_signature
from .service_outputs import root_key


def recycle(path):
    if os.name != 'nt':
        from send2trash import send2trash
        send2trash(str(Path(path).absolute()))
        return
    # Force modern IFileOperation with RECYCLEONDELETE; never use the legacy fallback.
    from send2trash.win.modern import send2trash
    send2trash(str(Path(path).absolute()))


class Maintenance:
    def __init__(self, engine):
        self.engine=engine; self.store=engine.store
        self.lock=threading.Lock(); self.cancel=threading.Event()
        self.result={'busy':False,'processed':0,'total':0,'items':[],'counts':{}}
        self.thread=None
        from .estimation import VerificationTime
        import time
        self.eta = VerificationTime(time.monotonic)

    def snapshot(self, offset=0):
        if not isinstance(offset, int) or not 0 <= offset <= 10000000:
            raise ValueError('Некорректная страница проверки')
        with self.lock:
            result = dict(self.result, items=[], counts=dict(self.result['counts']))
            result['eta_seconds'] = self.eta.estimate(result['processed'],result['total']) if result['busy'] else None
        run = result.get('run')
        if run:
            with self.store.db() as db:
                rows = db.execute('SELECT path,type,local,status FROM verification_results WHERE run=? ORDER BY path,type LIMIT 101 OFFSET ?', (run, offset)).fetchall()
            result.update(items=[dict(row) for row in rows[:100]], has_more=len(rows)>100, offset=offset)
        return result

    def repair_settings(self):
        with self.lock:
            result = dict(self.result)
            if result['busy'] or result.get('cancelled') or result.get('error') or not result.get('complete'):
                raise ValueError('Сначала завершите проверку папки без ошибок. Отменённая проверка содержит только часть результатов.')
            if not result.get('problems'):
                raise ValueError('Все проверенные результаты на месте. Дозагрузка не нужна.')
            # Recheck the complete original scope, never a truncated UI page.
            settings = dict(self.scope)
        return settings | {'operation':'missing', 'destination':result['root']}

    def start(self, data):
        with self.engine.lock:
            self.engine.ensure_idle()
            root=data.get('root')
            job_id=data.get('job')
            job=self.store.job(job_id) if job_id else None
            if job: root=job['root']
            if not isinstance(root,str) or not Path(root).is_absolute() or not Path(root).is_dir():
                raise ValueError('Выберите существующую папку загрузки')
            previous={'job':job,'store':self.store,'id':job_id} if job else find_import(self.store,root)
            if not previous: raise ValueError('В папке нет сохранённой описи загрузки')
            self.cancel.clear()
            self.eta.reset()
            self.scope = dict(previous['job']['settings'])
            with self.store.db() as db:
                db.execute('DELETE FROM verification_results')
            with self.lock:
                self.result={'busy':True,'run':uuid.uuid4().hex,'root':root,'processed':0,'total':0,'items':[],'counts':{},'error':'','complete':False,'problems':0,'current':'','checked_bytes':0}
            self.engine.maintenance_busy=True
            self.thread=threading.Thread(target=self.verify,args=(root,previous),name='disk-verify',daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.engine.maintenance_busy=False
                with self.lock: self.result['busy']=False
                raise
        return self.snapshot()

    def verify(self, root, previous):
        try:
            from .destination import DestinationGuard
            destination=DestinationGuard(root)
            _,error=destination.check(stopped=self.cancel.is_set)
            if error:
                with self.lock:self.result['error']=error
                return
            database=previous['store'].filename if previous.get('store') else previous['file']
            with closing(readonly(database)) as db:
                db.row_factory=sqlite3.Row
                where=' WHERE job=?' if previous.get('store') else ''
                args=(previous['id'],) if where else ()
                inventory = 'entries' if previous.get('store') else 'inventory'
                inventory_exists = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (inventory,)).fetchone())
                selection = 'SELECT path,type,local,size,sha256,1 AS recorded FROM artifacts'+where
                if inventory_exists:
                    condition = 'e.job=? AND ' if previous.get('store') else ''
                    match = 'a.job=e.job AND ' if previous.get('store') else ''
                    local = 'e.local' if previous.get('store') else '(SELECT n.local FROM names n WHERE n.identity=e.path)'
                    selection += " UNION ALL SELECT e.path,'original',"+local+",json_extract(e.meta,'$.size'),json_extract(e.meta,'$.sha256'),0 FROM "+inventory+" e WHERE "+condition+"e.kind='file' AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE "+match+"a.path=e.path)"
                    if previous.get('store'):
                        selection += " AND e.status!='filtered'"
                parameters = args + (args if inventory_exists else ())
                total=db.execute('SELECT COUNT(*) FROM ('+selection+')',parameters).fetchone()[0]
                with self.lock: self.result['total']=total
                rows = self.store.rows(selection+' ORDER BY path,type',parameters) if previous.get('store') else db.execute(selection+' ORDER BY path,type',parameters)
                pending = []
                for row in rows:
                    if self.cancel.is_set(): break
                    _,error=destination.check(stopped=self.cancel.is_set)
                    if error:
                        with self.lock:self.result['error']=error
                        return
                    status='ok'
                    try:
                        with self.lock: self.result['current']=row['path']
                        if not row['local']:
                            raise FileNotFoundError()
                        file=safe_join(root,row['local'])
                        before=file_signature(file, row['sha256'])
                        if not re.fullmatch('[a-fA-F0-9]{64}',row['sha256'] or ''): status='invalid_record'
                        elif before[2]!=row['size']: status='damaged'
                        else:
                            sha=hashlib.sha256()
                            with open(file,'rb') as stream:
                                while chunk:=stream.read(1024*1024):
                                    if self.cancel.is_set(): break
                                    sha.update(chunk)
                                    with self.lock: self.result['checked_bytes'] += len(chunk)
                            if self.cancel.is_set(): break
                            after=file_signature(file, row['sha256'])
                            if before!=after: status='changed_during_check'
                            elif sha.hexdigest()!=row['sha256'].lower(): status='damaged'
                    except FileNotFoundError: status='missing' if row['recorded'] else 'not_downloaded'
                    except (OSError,ValueError): status='unavailable'
                    if status in ('missing','unavailable'):
                        _,error=destination.check(stopped=self.cancel.is_set,force=True)
                        if error:
                            with self.lock:self.result['error']=error
                            return
                    with self.lock:
                        self.result['processed']+=1
                        self.eta.estimate(self.result['processed'],total)
                        self.result['counts'][status]=self.result['counts'].get(status,0)+1
                        if status!='ok':
                            self.result['problems'] += 1
                            pending.append((self.result['run'],row['path'],row['type'],row['local'],status))
                    if len(pending) >= 100:
                        with self.store.db() as target:
                            target.executemany('INSERT OR REPLACE INTO verification_results VALUES(?,?,?,?,?)',pending)
                        pending.clear()
                if pending:
                    with self.store.db() as target:
                        target.executemany('INSERT OR REPLACE INTO verification_results VALUES(?,?,?,?,?)',pending)
                with self.lock:
                    self.result['complete'] = not self.cancel.is_set() and self.result['processed'] == total
        except (OSError,ValueError,sqlite3.Error):
            with self.lock: self.result['error']='Приложение на ПК не смогло проверить опись или папку. Проверьте доступ к диску и запустите проверку снова.'
        except Exception as error:
            recorder=getattr(self.engine,'request_errors',None)
            if recorder:recorder.record('verification','local_files',error)
            with self.lock:self.result['error']='В приложении на ПК произошла ошибка проверки файлов. Отправьте диагностику через «Сообщить об ошибке» и повторите проверку.'
        finally:
            with self.lock:
                self.result['busy']=False; self.result['cancelled']=self.cancel.is_set()
            with self.engine.lock: self.engine.maintenance_busy=False

    def cleanup_preview(self, root):
        if not isinstance(root,str) or not Path(root).is_absolute() or not Path(root).is_dir():
            raise ValueError('Выберите папку загрузки')
        key=root_key(root)
        lexical=os.path.normcase(os.path.abspath(root))
        protected=set()
        with self.engine.lock:
            # Deletion already requires an idle engine. Do not inspect any
            # saved drive merely to resolve a legacy alias of an active job.
            if any(thread.is_alive() for thread in self.engine.threads.values()):
                return {'items':[],'bytes':0,'message':'Сейчас выполняется задание. Остановите его перед очисткой.'}
            for row in self.store.rows('SELECT id,root FROM jobs'):
                if os.path.normcase(os.path.abspath(row['root'])) not in (lexical,key): continue
                with self.store.db() as db:
                    queued=db.execute('SELECT 1 FROM job_queue WHERE job=?',(row['id'],)).fetchone()
                if queued: return {'items':[],'bytes':0,'message':'Папка используется очередью заданий'}
        # Preview does not mutate files. Slow IO belongs outside the global
        # control lock; cleanup() repeats all checks while exclusively idle.
        manifests=list(Path(root).glob('.disk-import*.sqlite3'))
        if len(manifests)>50:
            return {'items':[],'bytes':0,'message':'Слишком много описей. Очистка отключена для сохранности частей.'}
        for manifest in manifests:
            try:
                safe_join(root,manifest.name)
                with closing(readonly(manifest)) as source:
                    tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ('partials','artifacts'):
                        if table in tables:
                            protected.update(r[0] for r in source.execute('SELECT local FROM '+table) if isinstance(r[0],str))
            except (OSError,ValueError,sqlite3.Error):
                return {'items':[],'bytes':0,'message':'Опись недоступна или повреждена. Очистка отключена для сохранности частей.'}
        # Retain only actual candidate references, including legacy aliases,
        # using database metadata alone. Never probe other saved drives or
        # allocate a Python set for all media in the history.
        for reference in self.store.rows('''SELECT p.local FROM partials p JOIN temporary_files t ON t.root=? AND t.local=p.local
          UNION SELECT p.temp AS local FROM pending_publications p JOIN temporary_files t ON t.root=? AND t.local=p.temp
          UNION SELECT a.local FROM artifacts a JOIN temporary_files t ON t.root=? AND t.local=a.local''',(key,key,key)):
            protected.add(reference['local'])
        items=[]
        for row in self.store.rows('SELECT local,created,purpose FROM temporary_files WHERE root=?',(key,)):
            if row['local'] in protected or time.time()-row['created']<3600: continue
            try:
                file=safe_join(root,row['local']); info=os.stat(file)
                if info.st_nlink!=1 or not re.fullmatch(r'\.disk-[A-Za-z0-9_-]{8,}\.part',row['local']): continue
                items.append({'local':row['local'],'bytes':info.st_size,'signature':list(file_signature(file,''))})
            except (OSError,ValueError): continue
        return {'items':items,'bytes':sum(r['bytes'] for r in items),'message':'Только зарегистрированные временные файлы старше часа. Пригодные части защищены.'}

    def cleanup(self, data):
        root=data.get('root'); selected=data.get('items')
        if not isinstance(selected,list) or not selected or len(selected)>1000: raise ValueError('Выберите файлы для очистки')
        with self.engine.lock:
            self.engine.ensure_idle()
            allowed={r['local']:r for r in self.cleanup_preview(root)['items']}
            for item in selected:
                if not isinstance(item,dict) or item.get('local') not in allowed or item.get('signature')!=allowed[item['local']]['signature']:
                    raise ValueError('Список устарел. Повторите предварительный просмотр')
            removed=[]; failed=[]
            for item in selected:
                try:
                    file=safe_join(root,item['local'])
                    if list(file_signature(file,''))!=item['signature']:
                        raise ValueError('Файл изменён после предварительного просмотра')
                    recycle(file)
                    with self.store.db() as db: db.execute('DELETE FROM temporary_files WHERE root=? AND local=?',(root_key(root),item['local']))
                    removed.append(item['local'])
                except (OSError,ValueError): failed.append(item['local'])
            return {'removed':removed,'failed':failed}
