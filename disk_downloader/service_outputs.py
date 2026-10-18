import hashlib
import os
from pathlib import Path

from .paths import file_signature, reserve_output, safe_join, replace_owned, publish


def root_key(root):
    return os.path.normcase(str(Path(root).resolve()))


def digest_file(file, checkpoint=lambda: None):
    sha = hashlib.sha256()
    with open(file, 'rb') as stream:
        while chunk := stream.read(1024 * 1024):
            checkpoint()
            sha.update(chunk)
    return sha.hexdigest()


def location(store, job, kind, suggested, checkpoint=lambda: None):
    key = root_key(job['root'])
    with store.db() as db:
        row = db.execute('SELECT local,sha256 FROM service_outputs WHERE root=? AND kind=?', (key, kind)).fetchone()
    if row:
        file = safe_join(job['root'], row['local'])
        if not os.path.lexists(file) or os.stat(file).st_nlink == 1 and digest_file(file,checkpoint) == row['sha256']:
            return row['local'], bool(os.path.lexists(file))
    relative = reserve_output(store, job, suggested)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO service_outputs VALUES(?,?,?,?)',(key,kind,relative,''))
    return relative, False


def finish(store, job, kind, relative, temporary, replace, checkpoint=lambda: None):
    target = safe_join(job['root'], relative)
    with open(temporary, 'r+b') as stream:
        os.fsync(stream.fileno())
    sha = digest_file(temporary,checkpoint)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO pending_service_outputs VALUES(?,?,?,?)',(root_key(job['root']),kind,relative,sha))
    if replace:
        with store.db() as db:
            row = db.execute('SELECT sha256 FROM service_outputs WHERE root=? AND kind=? AND local=?', (root_key(job['root']), kind, relative)).fetchone()
        if not row or os.stat(target).st_nlink != 1 or digest_file(target,checkpoint) != row['sha256']:
            raise ValueError('Служебный файл изменён другим приложением')
        replace_owned(temporary, target)
    else:
        publish(temporary, target)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO service_outputs VALUES(?,?,?,?)', (root_key(job['root']), kind, relative, sha))
        db.execute('DELETE FROM pending_service_outputs WHERE root=? AND kind=?',(root_key(job['root']),kind))
        db.execute('DELETE FROM temporary_files WHERE root=? AND local=?',(root_key(job['root']),Path(temporary).name))
