import hashlib
import os
import re
from pathlib import Path

from .paths import safe_join, temporary_file


def strong_etag(value):
    return value if isinstance(value, str) and re.fullmatch(r'"[A-Za-z0-9._:+/-]{1,200}"', value) else ""


def valid_partial(row):
    return isinstance(row["local"], str) and re.fullmatch(r"\.disk-[A-Za-z0-9_-]{8,}\.part", row["local"]) and isinstance(row["size"], int) and 0 <= row["size"] < 2 ** 63 and isinstance(row["sha256"], str) and re.fullmatch(r"[a-fA-F0-9]{64}", row["sha256"])


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

    def discard(self, job, path, kind, remove=True, checkpoint=lambda: None):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM partials WHERE job=? AND path=? AND type=?", (job["id"], path, kind)).fetchone()
            owned = row and db.execute("SELECT 1 FROM partial_ownership WHERE job=? AND local=?", (job["id"], row["local"])).fetchone()
        if row and remove and owned and valid_partial(row):
            try:
                filename = safe_join(job["root"], row["local"])
                if os.stat(filename).st_nlink == 1 and os.path.getsize(filename) == row["size"] and digest_prefix(filename, row["size"], checkpoint)[0].hexdigest() == row["sha256"]:
                    os.unlink(filename)
            except (OSError, ValueError):
                pass
        with self.store.db() as db:
            db.execute("DELETE FROM partials WHERE job=? AND path=? AND type=?", (job["id"], path, kind))
            if row:
                db.execute("DELETE FROM partial_ownership WHERE job=? AND local=?", (job["id"], row["local"]))

    def prepare(self, job, path, kind, stamp, checkpoint):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM partials WHERE job=? AND path=? AND type=?", (job["id"], path, kind)).fetchone()
            owned = row and db.execute("SELECT 1 FROM partial_ownership WHERE job=? AND local=?", (job["id"], row["local"])).fetchone()
        if row:
            row = dict(row)
            try:
                if not valid_partial(row):
                    raise ValueError("Некорректная сохранённая часть")
                filename = safe_join(job["root"], row["local"])
                if os.stat(filename).st_nlink != 1:
                    raise ValueError("Сохранённая часть является жёсткой ссылкой")
                if row["fingerprint"] != stamp or os.path.getsize(filename) < row["size"]:
                    raise ValueError("Источник изменён")
                sha, md5 = digest_prefix(filename, row["size"], checkpoint)
                if sha.hexdigest() != row["sha256"]:
                    raise ValueError("Сохранённая часть изменена")
                if not owned:
                    replacement = temporary_file(job["root"], self.store, 'partial')
                    try:
                        copied = hashlib.sha256()
                        with open(filename, "rb") as source, open(replacement, "wb") as target:
                            remaining = row["size"]
                            while remaining:
                                checkpoint()
                                chunk = source.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ValueError("Часть изменилась во время копирования")
                                target.write(chunk)
                                copied.update(chunk)
                                remaining -= len(chunk)
                            target.flush()
                            os.fsync(target.fileno())
                        if copied.hexdigest() != row["sha256"]:
                            raise ValueError("Часть изменилась во время копирования")
                        row["local"] = Path(replacement).name
                        with self.store.db() as db:
                            db.execute("UPDATE partials SET local=? WHERE job=? AND path=? AND type=?", (row["local"], job["id"], path, kind))
                            db.execute("INSERT OR IGNORE INTO partial_ownership VALUES(?,?)", (job["id"], row["local"]))
                        return row, replacement, sha, md5
                    except BaseException:
                        os.unlink(replacement)
                        raise
                with open(filename, "r+b") as stream:
                    stream.truncate(row["size"])
                return row, filename, sha, md5
            except (OSError, ValueError):
                self.discard(job, path, kind, checkpoint=checkpoint)
        filename = temporary_file(job["root"], self.store, 'partial')
        row = {"job": job["id"], "path": path, "type": kind, "local": Path(filename).name, "fingerprint": stamp, "size": 0, "sha256": hashlib.sha256().hexdigest(), "etag": "", "total": None}
        with self.store.db() as db:
            db.execute("INSERT INTO partials VALUES(?,?,?,?,?,?,?,?,?)", tuple(row.values()))
            db.execute("INSERT INTO partial_ownership VALUES(?,?)", (job["id"], row["local"]))
        return row, filename, hashlib.sha256(), hashlib.md5()

    def save(self, job, path, kind, size, sha, etag, total):
        with self.store.db() as db:
            db.execute("UPDATE partials SET size=?,sha256=?,etag=?,total=? WHERE job=? AND path=? AND type=?", (size, sha.hexdigest(), strong_etag(etag), total, job["id"], path, kind))
