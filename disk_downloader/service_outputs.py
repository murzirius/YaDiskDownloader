import hashlib
import json
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


def unchanged(store, key, kind, relative, file, sha):
    """Prove ownership from a verified signature without reading an old log.

    Missing/weak metadata never grants permission to overwrite a file. Legacy
    outputs and files edited elsewhere are preserved under their existing name.
    """
    with store.db() as db:
        cached = db.execute('SELECT signature FROM service_output_signatures WHERE root=? AND kind=? AND local=?', (key, kind, relative)).fetchone()
    if not cached:
        return False
    signature = file_signature(file, sha)
    return bool(signature[-1] is not None and os.stat(file).st_nlink == 1 and json.dumps(signature) == cached['signature'])


def remember(store, key, kind, relative, file, sha, checkpoint=lambda: None):
    # Bind the signature to the bytes just published, not to an unchecked file
    # that another process might have replaced between rename and DB commit.
    before = file_signature(file, sha)
    if before[-1] is not None and os.stat(file).st_nlink == 1 and digest_file(file, checkpoint) == sha and file_signature(file, sha) == before:
        with store.db() as db:
            db.execute('INSERT OR REPLACE INTO service_output_signatures VALUES(?,?,?,?)', (key, kind, relative, json.dumps(before)))


def location(store, job, kind, suggested, checkpoint=lambda: None):
    key = root_key(job['root'])
    with store.db() as db:
        row = db.execute('SELECT local,sha256 FROM service_outputs WHERE root=? AND kind=?', (key, kind)).fetchone()
    if row:
        file = safe_join(job['root'], row['local'])
        checkpoint()
        if not os.path.lexists(file) or unchanged(store, key, kind, row['local'], file, row['sha256']):
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
        checkpoint()
        if not row or not unchanged(store, root_key(job['root']), kind, relative, target, row['sha256']):
            raise ValueError('Служебный файл изменён другим приложением')
        replace_owned(temporary, target)
    else:
        publish(temporary, target)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO service_outputs VALUES(?,?,?,?)', (root_key(job['root']), kind, relative, sha))
        db.execute('DELETE FROM pending_service_outputs WHERE root=? AND kind=?',(root_key(job['root']),kind))
        db.execute('DELETE FROM temporary_files WHERE root=? AND local=?',(root_key(job['root']),Path(temporary).name))
    remember(store, root_key(job['root']), kind, relative, target, sha, checkpoint)
