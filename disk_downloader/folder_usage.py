import os
import threading
import time
from pathlib import Path
from .paths import native


class FolderUsage:
    def __init__(self, interval=10):
        self.interval = interval
        self.lock = threading.Lock()
        self.folders = {}

    def snapshot(self, root, force=False):
        try:
            root = os.fspath(root)
        except TypeError:
            root = ''
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            return {"bytes":None,"updated":None,"scanning":False}
        root = str(Path(root).absolute())
        with self.lock:
            item = self.folders.setdefault(root, {"bytes": None, "updated": None, "scanning": False, "due": 0})
            if not item["scanning"] and (force or time.monotonic() >= item["due"]):
                item["scanning"] = True
                threading.Thread(target=self.scan, args=(root,), daemon=True, name="disk-folder-usage").start()
            return dict(item)

    def scan(self, root):
        total = 0
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
                            continue
            except OSError:
                continue
        with self.lock:
            self.folders[root].update(bytes=total, updated=time.time(), due=time.monotonic()+self.interval, scanning=False)
