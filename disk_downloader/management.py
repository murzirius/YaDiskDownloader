import json
import threading
from pathlib import Path


class Management:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.stopped = threading.Event()
        self.thread = None
        # Restart never starts queued downloads before an explicit connection.
        self.armed = False

    def paused(self):
        with self.store.db() as db:
            row = db.execute("SELECT value FROM preferences WHERE key='queue_paused'").fetchone()
        return bool(row and row[0] == 'true')

    def queue_snapshot(self):
        return {'paused': self.paused(), 'waiting_for_connection': not self.engine.api.credentials.connected,
                'items': [dict(r) for r in self.store.rows('SELECT q.*,j.title,j.phase FROM job_queue q JOIN jobs j ON j.id=q.job ORDER BY position')]}

    def queue_action(self, data):
        verb = data.get('action')
        with self.engine.lock:
            if self.engine.shutting_down:
                raise ValueError('Приложение завершается')
            with self.store.db() as db:
                if verb in ('pause', 'resume'):
                    db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused',?)", ('true' if verb == 'pause' else 'false',))
                    if verb == 'resume': self.armed = True
                elif verb == 'add':
                    job = self.store.job(data.get('job'))
                    if job['settings'].get('_invalid') or job['archived']:
                        raise ValueError('Задание недоступно для очереди')
                    if self.engine.threads.get(job['id']) and self.engine.threads[job['id']].is_alive():
                        raise ValueError('Задание уже выполняется')
                    if db.execute('SELECT 1 FROM job_queue WHERE job=?', (job['id'],)).fetchone():
                        raise ValueError('Задание уже в очереди')
                    position = db.execute('SELECT COALESCE(MAX(position),0)+1 FROM job_queue').fetchone()[0]
                    db.execute("INSERT INTO job_queue(job,position,action) VALUES(?,?,'resume')", (job['id'],position))
                    self.armed = True
                elif verb == 'remove':
                    row = db.execute('SELECT state FROM job_queue WHERE job=?', (data.get('job'),)).fetchone()
                    if row and row[0] == 'running': raise ValueError('Сначала остановите выполняющееся задание')
                    db.execute('DELETE FROM job_queue WHERE job=?', (data.get('job'),))
                elif verb == 'reorder':
                    order = data.get('jobs')
                    ids = [r[0] for r in db.execute('SELECT job FROM job_queue')]
                    if not isinstance(order,list) or any(not isinstance(i,str) for i in order) or len(order) != len(set(order)) or set(order) != set(ids):
                        raise ValueError('Передайте полный порядок очереди без повторов')
                    for position, job_id in enumerate(order):
                        db.execute('UPDATE job_queue SET position=? WHERE job=?', (position,job_id))
                else:
                    raise ValueError('Неизвестная команда очереди')
        return self.queue_snapshot()

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self.watch, name='disk-queue',daemon=True)
        self.thread.start()

    def watch(self):
        while not self.stopped.wait(.5):
            try: self.tick()
            except Exception:
                # Keep scheduler alive; errors of a specific task are recorded by tick.
                continue

    def tick(self):
        with self.engine.lock:
            if self.engine.shutting_down or self.paused() or not self.armed or not self.engine.api.credentials.connected:
                return
            with self.store.db() as db:
                for row in db.execute("SELECT job FROM job_queue WHERE state='running'").fetchall():
                    thread = self.engine.threads.get(row['job'])
                    if thread and thread.is_alive(): return
                    job = self.store.job(row['job'])
                    if job['phase'] in ('completed','completed_with_errors'):
                        db.execute('DELETE FROM job_queue WHERE job=?', (row['job'],))
                    else:
                        db.execute("UPDATE job_queue SET state='blocked',error=? WHERE job=?", (job['error'] or 'Задание остановлено. Решите проблему и добавьте его снова.',row['job']))
                        db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','true')")
                        return
            if any(t.is_alive() for t in self.engine.threads.values()): return
            with self.store.db() as db:
                row = db.execute("SELECT job FROM job_queue WHERE state='waiting' ORDER BY position LIMIT 1").fetchone()
            if not row: return
            try:
                self.engine.action(row['job'], 'resume')
                with self.store.db() as db:
                    db.execute("UPDATE job_queue SET state='running',error='' WHERE job=?", (row['job'],))
            except (ValueError,OSError) as exc:
                with self.store.db() as db:
                    db.execute("UPDATE job_queue SET state='blocked',error=? WHERE job=?", (str(exc),row['job']))
                    db.execute("INSERT OR REPLACE INTO preferences VALUES('queue_paused','true')")

    def history(self, query='', phase='all', archived=False, offset=0):
        if phase != 'all' and phase not in ('new','scanning','preparing','ready','downloading','paused','stopping','stopped','interrupted','packing','completed','completed_with_errors','failed'):
            raise ValueError('Некорректный статус')
        if not isinstance(query,str) or len(query)>200 or not isinstance(offset,int) or not 0<=offset<=1000000:
            raise ValueError('Некорректный поиск')
        sql = " FROM jobs WHERE archived=? AND (?='all' OR phase=?) AND (instr(lower(title),lower(?))>0 OR instr(lower(root),lower(?))>0 OR instr(id,?)>0)"
        args = (int(archived),phase,phase,query,query,query)
        with self.store.db() as db:
            total = db.execute('SELECT COUNT(*)'+sql,args).fetchone()[0]
            rows = db.execute('SELECT id,title,phase,root,started,archived'+sql+' ORDER BY started DESC LIMIT 30 OFFSET ?', (*args,offset)).fetchall()
        return {'items':[dict(r) for r in rows], 'total':total,'offset':offset,'has_more':offset+len(rows)<total}

    def history_action(self, job_id, data):
        with self.engine.lock:
            job = self.store.job(job_id)
            thread = self.engine.threads.get(job_id)
            if thread and thread.is_alive(): raise ValueError('Операция недоступна для активного задания')
            with self.store.db() as db:
                if db.execute('SELECT 1 FROM job_queue WHERE job=?', (job_id,)).fetchone():
                    raise ValueError('Сначала уберите задание из очереди')
                action = data.get('action')
                if action == 'rename':
                    title = data.get('title')
                    if not isinstance(title,str) or len(title.strip())>80 or any(ord(c)<32 for c in title):
                        raise ValueError('Название: до 80 символов')
                    db.execute('UPDATE jobs SET title=? WHERE id=?', (title.strip(),job_id))
                elif action in ('archive','unarchive'):
                    db.execute('UPDATE jobs SET archived=? WHERE id=?', (int(action=='archive'),job_id))
                elif action == 'delete':
                    if data.get('confirm') != job_id:
                        raise ValueError('Подтвердите удаление записи задания')
                    # Preserve journal events and root-level report/output ownership for the folder.
                    for table in ('entries','names','artifacts','partials','partial_ownership','pending_publications','baseline','changes','verification_cache','scan_errors'):
                        db.execute('DELETE FROM '+table+' WHERE job=?', (job_id,))
                    db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
                    self.engine.controls.pop(job_id,None); self.engine.threads.pop(job_id,None)
                else: raise ValueError('Неизвестная команда истории')
        return {'ok':True}
