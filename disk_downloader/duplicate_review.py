"""Explicit, selected-only duplicate cleanup; scans never start this worker."""
import threading

from .api import Suspended
from .engine import Control, safe_error
from .duplicates import recycle_duplicate
from .import_state import save_import
from .reports import write_reports
from .journal import Journal


class DuplicateReview:
    def __init__(self,engine):
        self.engine=engine;self.store=engine.store;self.lock=threading.Lock()
        self.control=None;self.thread=None
        self.result={'busy':False,'job':None,'processed':0,'total':0,'removed':0,'skipped':0,'error':'','current':'','cancelled':False}

    def snapshot(self,job_id,offset=0):
        self.store.job(job_id)
        if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=10000000:raise ValueError('Некорректная страница дубликатов.')
        with self.store.db() as db:
            rows=db.execute('SELECT local,canonical,type,size,sha256 FROM duplicate_candidates WHERE job=? ORDER BY local LIMIT 101 OFFSET ?',(job_id,offset)).fetchall()
            total=db.execute('SELECT COUNT(*) FROM duplicate_candidates WHERE job=?',(job_id,)).fetchone()[0]
        with self.lock:status=dict(self.result)
        return {'items':[dict(row) for row in rows[:100]],'has_more':len(rows)>100,'offset':offset,'total':total,'cleanup':status,'can_cleanup':not self.engine.shutting_down and not self.engine.maintenance_busy and not any(thread.is_alive() for thread in tuple(self.engine.threads.values()))}

    def start(self,job_id,data):
        items=data.get('items')
        if data.get('confirmation')!='recycle-selected':raise ValueError('Сначала выберите копии и подтвердите их перенос в корзину.')
        if not isinstance(items,list) or not 1<=len(items)<=1000:raise ValueError('Выберите от 1 до 1000 лишних копий.')
        selected={}
        for item in items:
            if not isinstance(item,dict) or not isinstance(item.get('local'),str):raise ValueError('Список дубликатов повреждён. Откройте его снова.')
            selected[item['local']]=item
        with self.engine.lock:
            if self.engine.shutting_down:raise ValueError('Приложение завершается.')
            self.engine.ensure_idle()
            self.store.job(job_id)
            with self.store.db() as db:
                records=[]
                for local,item in selected.items():
                    row=db.execute('SELECT local,canonical,type,size,sha256 FROM duplicate_candidates WHERE job=? AND local=?',(job_id,local)).fetchone()
                    if not row or dict(row)!=item:raise ValueError('Список дубликатов изменился. Обновите его и выберите копии заново.')
                    records.append(dict(row))
            self.control=Control()
            with self.lock:self.result=dict(busy=True,job=job_id,processed=0,total=len(records),removed=0,skipped=0,error='',current='',cancelled=False,zip_outdated=False)
            self.engine.maintenance_busy=True
            self.thread=threading.Thread(target=self.run,args=(job_id,records,self.control),name='disk-duplicate-cleanup',daemon=True)
            try:self.thread.start()
            except RuntimeError:
                self.engine.maintenance_busy=False
                with self.lock:self.result.update(busy=False,error='Приложение на устройстве не смогло начать очистку. Повторите позже.')
                raise ValueError('Приложение на устройстве не смогло начать очистку. Повторите позже.')
        return self.snapshot(job_id)

    def cancel(self):
        with self.lock:
            if self.control:self.control.stop()

    def run(self,job_id,records,control):
        journal=None;changed=False
        try:
            job=self.store.job(job_id);journal=Journal(self.engine,job_id);control.journal=journal
            # Canonical paths are strictly smaller; process leaves first so a
            # selected former canonical can be removed only after its dependants.
            for item in sorted(records,key=lambda row:row['local'],reverse=True):
                control.check()
                with self.lock:self.result['current']=item['local']
                with self.store.db() as db:
                    duplicate=db.execute('SELECT * FROM artifacts WHERE job=? AND local=? AND type=? AND sha256=? AND size=? LIMIT 1',(job_id,item['local'],item['type'],item['sha256'],item['size'])).fetchone()
                    canonical=db.execute('SELECT * FROM artifacts WHERE job=? AND local=? AND type=? AND sha256=? AND size=? LIMIT 1',(job_id,item['canonical'],item['type'],item['sha256'],item['size'])).fetchone()
                removed=False
                if duplicate and canonical and item['canonical']<item['local']:
                    try:removed=recycle_duplicate(self.engine,job,dict(duplicate),dict(canonical),control)
                    except (OSError,ValueError):pass
                if removed:
                    changed=True
                    with self.store.db() as db:db.execute('DELETE FROM duplicate_candidates WHERE job=? AND local=?',(job_id,item['local']))
                    control.event('cache','Выбранный дубликат перемещён в корзину. Сохранена копия: '+item['canonical'],duplicate['path'],'success')
                with self.lock:
                    self.result['processed']+=1;self.result['removed' if removed else 'skipped']+=1
            with self.lock:self.result['current']='Сохраняем историю и отчёт…' if changed else ''
        except Suspended:
            with self.lock:self.result['cancelled']=True
        except Exception as exc:
            with self.lock:self.result['error']='Очистка файлов на устройстве: '+safe_error(exc)
        finally:
            if changed:
                try:
                    if self.store.job(job_id).get('zip_path'):
                        self.store.update_job(job_id,zip_path=None,zip_bytes=0,zip_done=0,zip_total=0)
                        with self.lock:self.result['zip_outdated']=True
                    # Finish durable metadata even when cancellation was requested.
                    save_import(self.store,job_id)
                    write_reports(self.store,job_id)
                except Exception as exc:
                    with self.lock:self.result['error']='Файлы обработаны, но не удалось обновить опись или отчёт: '+safe_error(exc)+'. История в приложении сохранена.'
            if journal:
                try:journal.close()
                except Exception:pass
            try:
                self.engine.status_cache.invalidate()
                self.engine.folder_usage.for_job(self.store, job_id, self.store.job(job_id)['root'],force=True)
            finally:
                with self.lock:self.result.update(busy=False,current='')
                with self.engine.lock:self.engine.maintenance_busy=False
