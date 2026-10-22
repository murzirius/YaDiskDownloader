import os
import shutil
import stat
import sqlite3
import threading
import time
from pathlib import Path
from .paths import native, safe_join


class FolderUsage:
    def __init__(self, interval=300):
        self.interval = interval
        self.lock = threading.Lock()
        self.folders = {}

    def for_job(self, store, job_id, root, force=False, active=False):
        # Inventory contains media only. Never discover files by walking logs,
        # reports, temporary folders or unrelated content during a job refresh.
        def files():
            if not os.path.isdir(native(root)):
                raise OSError('Папка загрузки недоступна')
            for row in store.rows('SELECT DISTINCT local FROM artifacts WHERE job=?', (job_id,)):
                yield safe_join(root, row['local'])
        return self.snapshot(root, force=force, active=active, inventory=(job_id, files))

    def snapshot(self, root, force=False, active=False, inventory=None):
        try:
            root = os.fspath(root)
        except TypeError:
            root = ''
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            return {"bytes":None,"updated":None,"scanning":False}
        root = str(Path(root).absolute())
        with self.lock:
            item = self.folders.setdefault(root, {"bytes": None, "updated": None, "scanning": False, "due": 0, "error":"", "checked":0, 'generation':0,'dirty':False})
            scope = inventory[0] if inventory else None
            if item.get('scope') != scope:
                item.update(scope=scope, bytes=None, checked=0, dirty=True, partial=True, counted_files=0)
                item['generation'] += 1
            item['inventory'] = inventory[1] if inventory else None
            if force:
                item['generation']=item.get('generation',0)+1
                item['dirty']=True
            due = item['checked'] + (min(30, self.interval) if active else self.interval)
            if not item["scanning"] and sum(value['scanning'] for value in self.folders.values()) < 2 and (item.get('dirty') or not item['checked'] or time.monotonic() >= due):
                item["scanning"] = True
                try:
                    threading.Thread(target=self.scan, args=(root,item.get('generation',0),item['inventory']), daemon=True, name="disk-folder-usage").start()
                except RuntimeError:
                    item.update(scanning=False,error='На ПК не удалось начать подсчёт размера папки. Повторите обновление объёма позже.')
            while len(self.folders)>64:
                victim=next((key for key,value in self.folders.items() if key!=root and not value['scanning']),None)
                if victim is None: break
                del self.folders[victim]
            return dict(item,scanning=item['scanning'] or item.get('dirty',False))

    def scan(self, root, generation=None, inventory=None):
        if generation is None:
            with self.lock: generation=self.folders[root].get('generation',0)
        total = 0
        free = None
        try:free = shutil.disk_usage(native(root)).free
        except OSError:pass
        error = ''
        counted = 0
        last_publish = 0
        def progress():
            nonlocal last_publish
            current = time.monotonic()
            if current-last_publish < .25:return
            last_publish = current
            with self.lock:
                item = self.folders[root]
                if item.get('generation',0) == generation:
                    item['counted_files'] = counted
                    if item['bytes'] is None or item.get('partial'):
                        item.update(bytes=total,partial=True,updated=time.time())
        if inventory:
            try:
                for filename in inventory():
                    try:
                        info = os.stat(filename, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):continue
                        total += info.st_size
                        counted += 1
                        progress()
                    except FileNotFoundError:
                        continue
            except (OSError, ValueError, sqlite3.Error):
                error = 'Часть скачанных файлов недоступна. Проверьте подключение диска и доступ к папке.'
        queue = [] if inventory else [native(root)]
        while queue:
            directory = queue.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if not getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400 and not (hasattr(Path, "is_junction") and Path(entry.path).is_junction()):
                                    queue.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            error = 'Часть файлов на ПК недоступна. Размер папки удалось проверить не полностью.'
            except OSError:
                error = 'Папка на ПК недоступна. Проверьте подключение диска и права доступа.'
        with self.lock:
            item=self.folders[root]
            stale = item.get('generation',0) != generation
            item.update(scanning=False, dirty=stale)
            if not stale:
                item.update(bytes=None if error else total, free_bytes=free, partial=False, counted_files=counted, updated=time.time(), due=time.monotonic()+self.interval, checked=time.monotonic(), error=error)
