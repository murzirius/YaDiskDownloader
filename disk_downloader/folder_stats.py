"""Explicit, cancellable folder totals. The traversal queue lives on disk."""
from collections import OrderedDict
from contextlib import closing
from datetime import datetime
import sqlite3
import tempfile
import threading
import time

from .api import APIError, Suspended, validate_resource
from .paths import parent_path, remote_path


class FolderStats:
    def __init__(self, api):
        self.api = api
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = None
        self.cache = OrderedDict()
        self.result = {'busy': False, 'files': 0, 'items': {}, 'error': ''}

    def snapshot(self, paths=None):
        epoch = self.api.credentials.status()['auth_revision']
        with self.lock:
            result = dict(self.result, items={})
            result['items'] = {path: dict(value) for (generation, path), value in self.cache.items() if generation == epoch and (paths is None or path in paths)}
            return result

    def start(self, paths):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
            raise ValueError('Выберите от 1 до 100 папок для подсчёта')
        paths = list(dict.fromkeys(remote_path(path) for path in paths))
        _, epoch = self.api.credentials.snapshot()
        if not self.api.credentials.connected:
            raise ValueError('Подключите Яндекс.Диск перед подсчётом папок')
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('Подсчёт папок уже выполняется. Дождитесь результата или отмените его.')
            self.cancel.clear()
            self.result = {'busy': True, 'files': 0, 'items': {}, 'error': ''}
            self.thread = threading.Thread(target=self.run, args=(paths, epoch), name='disk-folder-totals', daemon=True)
            try:self.thread.start()
            except RuntimeError:
                self.result['busy']=False
                raise ValueError('Приложение не смогло начать подсчёт. Повторите позже.') from None
        return self.snapshot(paths)

    def check(self, epoch):
        if self.cancel.is_set():
            raise Suspended('Подсчёт папок остановлен')
        self.api.credentials.check(epoch)

    def run(self, paths, epoch):
        try:
            for root in paths:
                self.check(epoch)
                value = {'bytes': 0, 'files': 0, 'unknown_sizes': 0, 'latest_modified': None, 'complete': True, 'date_complete': True, 'checked_at': time.time()}
                latest = None
                with tempfile.TemporaryDirectory(prefix='yadisk-folder-totals-') as folder, closing(sqlite3.connect(folder+'/queue.sqlite3')) as queue:
                    queue.execute('CREATE TABLE pending(path TEXT PRIMARY KEY,offset INTEGER NOT NULL DEFAULT 0)')
                    queue.execute('CREATE TABLE seen(path TEXT PRIMARY KEY)')
                    queue.execute('INSERT INTO pending(path) VALUES(?)', (root,))
                    while row := queue.execute('SELECT path,offset FROM pending ORDER BY path LIMIT 1').fetchone():
                        self.check(epoch)
                        path, offset = row
                        try:
                            response = self.api.metadata(path, {'attempts': 1, 'timeout': 12, 'total_timeout': 20, 'preview_size': 'S'}, lambda:self.check(epoch), offset=offset, limit=1000)
                            page = response.get('_embedded')
                            if not isinstance(page, dict) or not isinstance(page.get('items'), list):
                                raise APIError('Яндекс.Диск не вернул список файлов папки')
                            items, total = page['items'], page.get('total')
                            if len(items)>1000 or total is not None and (type(total) is not int or total<0) or not items and total is not None and offset<total:
                                raise APIError('Яндекс.Диск вернул неполную страницу папки')
                            for item in items:
                                self.check(epoch)
                                validate_resource(item)
                                if item['path'] == path or parent_path(item['path']) != path:
                                    raise APIError('Яндекс.Диск вернул файл из другой папки')
                                if not queue.execute('INSERT OR IGNORE INTO seen VALUES(?)',(item['path'],)).rowcount:
                                    raise APIError('Яндекс.Диск повторил объект при подсчёте. Итог неполный.')
                                if item['type'] == 'dir':
                                    queue.execute('INSERT OR IGNORE INTO pending(path) VALUES(?)', (item['path'],))
                                else:
                                    value['files'] += 1
                                    if item.get('size') is None:value['unknown_sizes'] += 1
                                    else:value['bytes'] += item['size']
                                    try:
                                        stamp = datetime.fromisoformat(item.get('modified', '').replace('Z','+00:00'))
                                        if stamp.tzinfo is None:value['date_complete'] = False
                                        elif latest is None or stamp > latest:latest = stamp
                                    except (ValueError, TypeError, AttributeError):
                                        value['date_complete'] = False
                            next_offset = offset+len(items)
                            if not items or total is not None and next_offset>=total:queue.execute('DELETE FROM pending WHERE path=?', (path,))
                            else:queue.execute('UPDATE pending SET offset=? WHERE path=?', (next_offset,path))
                            queue.commit()
                            with self.lock:self.result['files'] += len([item for item in items if item['type']=='file'])
                        except APIError:
                            value['complete'] = False
                            queue.execute('DELETE FROM pending WHERE path=?', (path,))
                            queue.commit()
                value['latest_modified'] = latest.isoformat() if latest else None
                value['complete'] = value['complete'] and value['unknown_sizes']==0
                with self.lock:
                    self.cache[(epoch,root)] = value
                    self.cache.move_to_end((epoch,root))
                    while len(self.cache)>256:self.cache.popitem(last=False)
        except Suspended:
            with self.lock:self.result['error'] = 'Подсчёт остановлен. Завершённые результаты сохранены.'
        except Exception:
            with self.lock:self.result['error'] = 'Приложение не смогло посчитать папки. Повторите подсчёт; файлы на Диске не изменялись.'
        finally:
            with self.lock:self.result['busy'] = False
