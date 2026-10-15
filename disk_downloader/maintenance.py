import hashlib
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from .backups import readonly
from .import_state import find_import
from .paths import safe_join, file_signature
from .service_outputs import root_key, digest_file


def recycle(path):
    if os.name != 'nt':
        raise ValueError('Очистка через корзину поддерживается в Windows')
    # Force modern IFileOperation with RECYCLEONDELETE; never use the legacy fallback.
    from send2trash.win.modern import send2trash
    send2trash(str(Path(path).absolute()))


class Maintenance:
    def __init__(self, engine):
        self.engine=engine; self.store=engine.store
        self.lock=threading.Lock(); self.cancel=threading.Event()
        self.result={'busy':False,'processed':0,'total':0,'items':[],'counts':{}}
        self.thread=None

    def snapshot(self):
        with self.lock:
            return dict(self.result,items=list(self.result['items']),counts=dict(self.result['counts']))

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
            self.cancel.clear(); self.engine.maintenance_busy=True
            with self.lock:
                self.result={'busy':True,'root':root,'processed':0,'total':0,'items':[],'counts':{},'error':''}
            self.thread=threading.Thread(target=self.verify,args=(root,previous),name='disk-verify',daemon=True)
            self.thread.start()
        return self.snapshot()

    def verify(self, root, previous):
        try:
            database=previous['store'].filename if previous.get('store') else previous['file']
            with closing(readonly(database)) as db:
                db.row_factory=sqlite3.Row
                where=' WHERE job=?' if previous.get('store') else ''
                args=(previous['id'],) if where else ()
                total=db.execute('SELECT COUNT(*) FROM artifacts'+where,args).fetchone()[0]
                with self.lock: self.result['total']=total
                for row in db.execute('SELECT path,type,local,size,sha256 FROM artifacts'+where+' ORDER BY path,type',args):
                    if self.cancel.is_set(): break
                    status='ok'
                    try:
                        file=safe_join(root,row['local'])
                        before=os.stat(file)
                        if not re.fullmatch('[a-fA-F0-9]{64}',row['sha256'] or ''): status='invalid_record'
                        elif before.st_size!=row['size']: status='damaged'
                        else:
                            sha=hashlib.sha256()
                            with open(file,'rb') as stream:
                                while chunk:=stream.read(1024*1024):
                                    if self.cancel.is_set(): break
                                    sha.update(chunk)
                            if self.cancel.is_set(): break
                            after=os.stat(file)
                            if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino): status='changed_during_check'
                            elif sha.hexdigest()!=row['sha256'].lower(): status='damaged'
                    except FileNotFoundError: status='missing'
                    except (OSError,ValueError): status='unavailable'
                    with self.lock:
                        self.result['processed']+=1
                        self.result['counts'][status]=self.result['counts'].get(status,0)+1
                        if status!='ok' and len(self.result['items'])<2000:
                            self.result['items'].append({'path':row['path'],'type':row['type'],'local':row['local'],'status':status})
        except (OSError,ValueError,sqlite3.Error):
            with self.lock: self.result['error']='Не удалось прочитать опись или папку'
        finally:
            with self.lock:
                self.result['busy']=False; self.result['cancelled']=self.cancel.is_set()
            with self.engine.lock: self.engine.maintenance_busy=False

    def cleanup_preview(self, root):
        if not isinstance(root,str) or not Path(root).is_absolute() or not Path(root).is_dir():
            raise ValueError('Выберите папку загрузки')
        key=root_key(root)
        protected=set()
        with self.engine.lock:
            for row in self.store.rows('SELECT id,root FROM jobs'):
                if root_key(row['root'])!=key: continue
                if self.engine.threads.get(row['id']) and self.engine.threads[row['id']].is_alive():
                    return {'items':[],'bytes':0,'message':'В этой папке выполняется задание'}
                with self.store.db() as db:
                    queued=db.execute('SELECT 1 FROM job_queue WHERE job=?',(row['id'],)).fetchone()
                for part in self.store.rows('SELECT local FROM partials WHERE job=?',(row['id'],)):
                    protected.add(part['local'])
                for part in self.store.rows('SELECT temp FROM pending_publications WHERE job=?',(row['id'],)):
                    protected.add(part['temp'])
                for artifact in self.store.rows('SELECT local FROM artifacts WHERE job=?',(row['id'],)):
                    protected.add(artifact['local'])
                if queued: return {'items':[],'bytes':0,'message':'Папка используется очередью заданий'}
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
