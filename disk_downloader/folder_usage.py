import os
import threading
import time
from pathlib import Path
from .paths import native


class FolderUsage:
    def __init__(self, interval=300):
        self.interval = interval
        self.lock = threading.Lock()
        self.folders = {}

    def snapshot(self, root, force=False, active=False):
        try:
            root = os.fspath(root)
        except TypeError:
            root = ''
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            return {"bytes":None,"updated":None,"scanning":False}
        root = str(Path(root).absolute())
        with self.lock:
            item = self.folders.setdefault(root, {"bytes": None, "updated": None, "scanning": False, "due": 0, "error":"", "checked":0})
            due = item['checked'] + (min(30, self.interval) if active else self.interval)
            if not item["scanning"] and sum(value['scanning'] for value in self.folders.values()) < 2 and (force or not item['checked'] or time.monotonic() >= due):
                item["scanning"] = True
                try:
                    threading.Thread(target=self.scan, args=(root,), daemon=True, name="disk-folder-usage").start()
                except RuntimeError:
                    item.update(scanning=False,error='На ПК не удалось начать подсчёт размера папки. Повторите обновление объёма позже.')
            while len(self.folders)>64:
                victim=next((key for key,value in self.folders.items() if key!=root and not value['scanning']),None)
                if victim is None: break
                del self.folders[victim]
            return dict(item)

    def scan(self, root):
        total = 0
        error = ''
        queue = [native(root)]
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
            self.folders[root].update(bytes=None if error else total, updated=time.time(), due=time.monotonic()+self.interval, checked=time.monotonic(), scanning=False, error=error)
