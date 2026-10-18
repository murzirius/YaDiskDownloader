"""Recover a verified publication interrupted between rename and DB commit."""
import os
import threading
from .paths import safe_join
from .service_outputs import digest_file


def recover_publications(store, checkpoint=lambda: None, progress=lambda path: None):
    from .duplicates import recover_moves
    recovered = recover_moves(store,checkpoint,progress)
    for row in store.rows('SELECT * FROM pending_service_outputs'):
        checkpoint()
        progress(row['local'])
        try:
            target=safe_join(row['root'],row['local'])
            if os.path.isfile(target) and os.stat(target).st_nlink==1 and digest_file(target, checkpoint)==row['sha256']:
                with store.db() as db:
                    db.execute('INSERT OR REPLACE INTO service_outputs VALUES(?,?,?,?)',tuple(row[key] for key in ('root','kind','local','sha256')))
                    db.execute('DELETE FROM pending_service_outputs WHERE root=? AND kind=?',(row['root'],row['kind']))
        except (OSError,ValueError):
            continue
    for row in store.rows('SELECT p.*,j.root FROM pending_publications p JOIN jobs j ON j.id=p.job'):
        checkpoint()
        progress(row['local'])
        try:
            target = safe_join(row['root'], row['local'])
            if not os.path.isfile(target) or os.stat(target).st_nlink != 1 or os.path.getsize(target) != row['size'] or digest_file(target, checkpoint) != row['sha256']:
                continue
            with store.db() as db:
                db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)', tuple(row[k] for k in ('job','path','type','local','size','sha256','fingerprint','preview_size')))
                db.execute('DELETE FROM partials WHERE job=? AND path=? AND type=?', (row['job'],row['path'],row['type']))
                db.execute('DELETE FROM partial_ownership WHERE job=? AND local=?', (row['job'],row['temp']))
                db.execute('DELETE FROM pending_publications WHERE job=? AND path=? AND type=?', (row['job'],row['path'],row['type']))
            recovered += 1
        except (OSError, ValueError):
            continue
    return recovered


class Recovery:
    """Expose crash recovery immediately without serving conflicting commands."""
    def __init__(self, store):
        self.store=store
        self.lock=threading.Lock()
        self.cancel=threading.Event()
        self.thread=None
        self.result={'busy':False,'error':'','processed':0,'total':0,'current':'','recovered':0}

    def snapshot(self):
        with self.lock: return dict(self.result)

    def start(self):
        with self.lock:
            if self.result['busy']: return
            with self.store.db() as db:
                total=db.execute('SELECT (SELECT COUNT(*) FROM pending_publications)+(SELECT COUNT(*) FROM pending_service_outputs)+(SELECT COUNT(*) FROM duplicate_moves)').fetchone()[0]
            self.result.update(busy=bool(total),error='',processed=0,total=total,current='')
            if not total: return
            self.cancel.clear()
            self.thread=threading.Thread(target=self.run,name='disk-recovery',daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                self.result.update(busy=False,error='На ПК не удалось запустить восстановление. Закройте лишние программы и нажмите «Повторить восстановление».')

    def run(self):
        def checkpoint():
            if self.cancel.is_set(): raise InterruptedError('Восстановление остановлено')
        def progress(path):
            with self.lock:
                self.result['processed']+=1
                self.result['current']=path
        try:
            recovered=recover_publications(self.store,checkpoint,progress)
            with self.lock: self.result['recovered']=recovered
        except Exception:
            with self.lock: self.result['error']='Приложение на ПК не смогло завершить восстановление. Проверьте подключение диска и свободное место, затем нажмите «Повторить восстановление».'
        finally:
            with self.lock: self.result['busy']=False
