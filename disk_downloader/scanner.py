"""Bounded folder-page prefetch; only the owner thread writes progress."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json

from .api import APIError, Suspended, validate_resource
from .paths import PathError, parent_path, remote_path

PAGE_SIZE = 1000
SCAN_WORKERS = 4


def scan_pages(engine, job_id, settings, control):
    pending = {}
    cursors, ahead = {}, {}
    ignored = set()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix='disk-scan') as pool:
        try:
            while True:
                control.check()
                with engine.store.db() as db:
                    excluded = tuple(path for path, _ in pending.values())
                    sql = "SELECT path,scan_offset FROM entries WHERE job=? AND kind='dir' AND scanned=0"
                    if excluded:
                        sql += ' AND path NOT IN (' + ','.join('?' for _ in excluded) + ')'
                    rows = db.execute(sql+' ORDER BY path LIMIT ?', (job_id, *excluded, SCAN_WORKERS-len(pending))).fetchall()
                for path, offset in rows:
                    cursors[path] = offset
                    control.progress(path, offset, None, 'Читаем список файлов Яндекс.Диска', unit='objects')
                    future = pool.submit(engine.api.metadata, path, settings, control.check, offset=offset, limit=PAGE_SIZE)
                    pending[future] = (path, offset)
                if not pending:
                    break
                waiting = [future for future in pending if not future.done()]
                if waiting:
                    wait(waiting, timeout=.1, return_when=FIRST_COMPLETED)
                # Completed later pages stay bounded in pending until the
                # previous page is committed. A restart uses only that cursor.
                done = sorted((future for future in pending if future.done()), key=lambda future: pending[future])
                for future in done:
                    path, offset = pending[future]
                    if future in ignored:
                        pending.pop(future)
                        ignored.discard(future)
                        continue
                    if offset != cursors[path]:
                        continue
                    pending.pop(future)
                    try:
                        meta = future.result()
                        control.check()
                        embedded = meta.get('_embedded')
                        if not isinstance(embedded, dict) or not isinstance(embedded.get('items'), list):
                            raise APIError('Яндекс.Диск не вернул содержимое папки')
                        items, total = embedded['items'], embedded.get('total')
                        if len(items) > PAGE_SIZE or total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
                            raise APIError('Яндекс.Диск вернул некорректную страницу папки')
                        if not items and total is not None and offset < total:
                            raise APIError('Яндекс.Диск вернул пустую страницу до окончания списка. Повторите обход папки.')
                        children = set()
                        for item in items:
                            validate_resource(item)
                            child = remote_path(item.get('path', ''))
                            if child == path or parent_path(child) != path or child in children:
                                raise APIError('Яндекс.Диск вернул повторный или недопустимый дочерний путь')
                            children.add(child)
                        with engine.store.db() as db:
                            signature = hashlib.sha256(json.dumps(sorted(children)).encode()).hexdigest()
                            previous = db.execute('SELECT signature FROM scan_pages WHERE job=? AND path=?',(job_id,path)).fetchone()
                            if items and offset and previous and previous[0]==signature:
                                raise APIError('Яндекс.Диск повторяет прежнюю страницу папки. Повторите обход; неполный список не считается полным.')
                            if items and offset:
                                seen = db.execute('''SELECT COUNT(*) FROM json_each(?) c WHERE
                                  EXISTS(SELECT 1 FROM entries e WHERE e.job=? AND e.path=c.value)
                                  OR EXISTS(SELECT 1 FROM skipped_sources s WHERE s.job=? AND s.path=c.value)''',(json.dumps(list(children)),job_id,job_id)).fetchone()[0]
                                if seen == len(children):
                                    raise APIError('Яндекс.Диск повторяет уже просмотренные файлы. Повторите обход; список папки неполный.')
                            for item in items:
                                engine.store.insert_entry(db, job_id, item, settings)
                            next_offset = offset + len(items)
                            finished = not items or total is not None and next_offset >= total
                            db.execute('UPDATE entries SET scan_offset=?,scanned=? WHERE job=? AND path=?', (next_offset, int(finished), job_id, path))
                            db.execute('DELETE FROM scan_errors WHERE job=? AND path=?', (job_id, path))
                            db.execute('INSERT OR REPLACE INTO scan_pages VALUES(?,?,?)',(job_id,path,signature))
                        cursors[path] = next_offset
                        if finished:
                            ahead.pop(path, None)
                            cursors.pop(path, None)
                            for other, (name, _) in pending.items():
                                if name == path:
                                    ignored.add(other)
                        elif len(items) == PAGE_SIZE and total is not None:
                            scheduled = max((start + PAGE_SIZE for name, start in pending.values() if name == path), default=next_offset)
                            ahead[path] = (scheduled, total)
                        else:
                            # A server may return fewer items than requested.
                            # Discard speculative pages and follow its actual cursor.
                            ahead.pop(path, None)
                            for other, (name, _) in pending.items():
                                if name == path:
                                    ignored.add(other)
                        control.event('scan', f'Получено {len(items)} объектов; просмотрено {next_offset}' + (' · папка прочитана' if finished else ''), path)
                        with control.lock:
                            control.active.pop(path, None)
                    except (APIError, PathError) as error:
                        if isinstance(error, APIError) and error.status == 401:
                            raise Suspended(str(error))
                        from .engine import safe_error
                        message = safe_error(error)
                        with engine.store.db() as db:
                            db.execute('INSERT OR REPLACE INTO scan_errors VALUES(?,?,?)', (job_id, path, message))
                            db.execute('UPDATE entries SET scanned=1 WHERE job=? AND path=?', (job_id, path))
                        control.event('errors', 'Ошибка обхода папки: '+message, path, 'error')
                        ahead.pop(path, None)
                        cursors.pop(path, None)
                        for other, (name, _) in pending.items():
                            if name == path:
                                ignored.add(other)
                # The same four request slots can now serve successive pages
                # of one large folder, rather than waiting for it page by page.
                for path, (start, total) in list(ahead.items()):
                    while len(pending) < SCAN_WORKERS and start < total:
                        future = pool.submit(engine.api.metadata, path, settings, control.check, offset=start, limit=PAGE_SIZE)
                        pending[future] = (path, start)
                        start += PAGE_SIZE
                    ahead[path] = (start, total)
        finally:
            for future in pending:
                future.cancel()
