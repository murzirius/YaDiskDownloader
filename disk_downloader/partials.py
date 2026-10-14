import hashlib
import os
import re
from pathlib import Path

from .paths import safe_join, temporary_file


def strong_etag(value):
    return value if isinstance(value, str) and re.fullmatch(r'"[A-Za-z0-9._:+/-]{1,200}"', value) else ""


def digest_prefix(filename, size, checkpoint):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(filename, "rb") as stream:
        remaining = size
        while remaining:
            checkpoint()
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Неполная сохранённая часть")
            sha.update(chunk)
            md5.update(chunk)
            remaining -= len(chunk)
    return sha, md5


class Partials:
    def __init__(self, store):
        self.store = store

    def discard(self, job, path, kind, remove=True):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM partials WHERE job=? AND path=? AND type=?", (job["id"], path, kind)).fetchone()
        if row and remove:
            try:
                filename = safe_join(job["root"], row["local"])
                if os.path.getsize(filename) == row["size"] and digest_prefix(filename, row["size"], lambda: None)[0].hexdigest() == row["sha256"]:
                    os.unlink(filename)
            except (OSError, ValueError):
                pass
        with self.store.db() as db:
            db.execute("DELETE FROM partials WHERE job=? AND path=? AND type=?", (job["id"], path, kind))

    def prepare(self, job, path, kind, stamp, checkpoint):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM partials WHERE job=? AND path=? AND type=?", (job["id"], path, kind)).fetchone()
        if row:
            row = dict(row)
            try:
                filename = safe_join(job["root"], row["local"])
                if row["fingerprint"] != stamp or os.path.getsize(filename) < row["size"]:
                    raise ValueError("Источник изменён")
                sha, md5 = digest_prefix(filename, row["size"], checkpoint)
                if sha.hexdigest() != row["sha256"]:
                    raise ValueError("Сохранённая часть изменена")
                with open(filename, "r+b") as stream:
                    stream.truncate(row["size"])
                return row, filename, sha, md5
            except (OSError, ValueError):
                self.discard(job, path, kind)
        filename = temporary_file(job["root"])
        row = {"job": job["id"], "path": path, "type": kind, "local": Path(filename).name, "fingerprint": stamp, "size": 0, "sha256": hashlib.sha256().hexdigest(), "etag": "", "total": None}
        with self.store.db() as db:
            db.execute("INSERT INTO partials VALUES(?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        return row, filename, hashlib.sha256(), hashlib.md5()

    def save(self, job, path, kind, size, sha, etag, total):
        with self.store.db() as db:
            db.execute("UPDATE partials SET size=?,sha256=?,etag=?,total=? WHERE job=? AND path=? AND type=?", (size, sha.hexdigest(), strong_etag(etag), total, job["id"], path, kind))
