"""Recover a verified publication interrupted between rename and DB commit."""
import os
import re
import stat
import threading
from .paths import file_signature, safe_join
from .service_outputs import digest_file, remember, root_key


def _signature(file, sha):
    info = os.lstat(file)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Файл восстановления не является обычным файлом')
    signature = file_signature(file, sha)
    if signature[:5] != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
        raise ValueError('Файл изменился во время проверки')
    return signature + (info.st_nlink,)


def _verified(file, sha, checkpoint, size=None, before=None):
    before = _signature(file, sha) if before is None else before
    if before[-1] not in (1, 2) or size is not None and before[2] != size:
        return None
    if digest_file(file, checkpoint) != sha or _signature(file, sha) != before:
        return None
    return before


def _temporary_name(name):
    return isinstance(name, str) and re.fullmatch(r'\.disk-[A-Za-z0-9_-]{8,}\.part', name, re.IGNORECASE)


def _pair_matches(target, temporary, signature):
    # Weak file IDs or an extra alias never grant permission to unlink.
    return (signature[-1] == 2 and signature[0] and signature[1] and signature[-2] is not None
            and os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(temporary))
            and _signature(target, signature[5]) == signature
            and _signature(temporary, signature[5]) == signature)


def _finish_pair(target, temporary, signature, checkpoint):
    checkpoint()
    if not _pair_matches(target, temporary, signature):
        raise ValueError('Пара файлов восстановления изменилась')
    os.unlink(temporary)
    after = _signature(target, signature[5])
    # Removing the second owned name legitimately changes ctime/link count.
    # It must never change the inode, device, content size or modification time.
    if after[-1] != 1 or after[:4] != signature[:4]:
        raise ValueError('Готовый файл изменился при восстановлении')


def _media_owner(db, row, key):
    # Partial fingerprints identify the transfer variant, while publication
    # fingerprints identify the remote resource. Match the actual writer's
    # stamp, including preview size, rather than accepting an untyped stamp.
    if not isinstance(row['fingerprint'], str) or row['type'] not in ('original', 'preview'):
        return False
    stamp = row['fingerprint'] + ':' + row['type']
    if row['type'] == 'preview':
        if not isinstance(row['preview_size'], str):
            return False
        stamp += ':' + row['preview_size']
    return _temporary_name(row['temp']) and db.execute("""SELECT 1 FROM partials p
      JOIN partial_ownership o ON o.job=p.job AND o.local=p.local
      JOIN temporary_files t ON t.root=? AND t.local=p.local AND t.purpose='partial'
      WHERE p.job=? AND p.path=? AND p.type=? AND p.local=? AND p.fingerprint=?
      AND p.size=? AND p.sha256=?""", (key,row['job'],row['path'],row['type'],row['temp'],stamp,row['size'],row['sha256'])).fetchone() is not None


def _same_pending(current, row):
    return current is not None and all(current[key] == value for key,value in row.items())


def _service_purpose(kind):
    if kind in ('manifest', 'journal', 'changes'):
        return kind
    if kind in ('report:csv', 'report:json', 'report:html', 'report:txt'):
        return 'report'
    return None


def recover_publications(store, checkpoint=lambda: None, progress=lambda path: None):
    from .duplicates import recover_moves
    recovered = recover_moves(store,checkpoint,progress)
    for row in store.rows('SELECT * FROM pending_service_outputs'):
        checkpoint()
        progress(row['local'])
        try:
            target = safe_join(row['root'],row['local'])
            signature = _signature(target,row['sha256'])
            if signature[-1] not in (1, 2):
                continue
            temporary = None
            registered = []
            purpose = _service_purpose(row['kind'])
            if signature[-1] == 2:
                if purpose is None:
                    continue
                with store.db() as db:
                    registered = [r[0] for r in db.execute('SELECT local FROM temporary_files WHERE root=? AND purpose=? ORDER BY local',(row['root'],purpose))]
                candidates = []
                for name in registered:
                    checkpoint()
                    if not _temporary_name(name):
                        continue
                    try:
                        candidate = safe_join(row['root'],name)
                        if _pair_matches(target,candidate,signature):
                            candidates.append(candidate)
                    except (OSError,ValueError):
                        continue
                if len(candidates) != 1:
                    continue
                temporary = candidates[0]
            # Reject unknown/unowned pairs without reading their full contents.
            # Verified pairs still hash and recheck signatures before unlink.
            signature = _verified(target,row['sha256'],checkpoint,before=signature)
            if signature is None:
                continue
            checkpoint()
            with store.db() as db:
                # Acquire writer admission before re-reading intent. A SELECT
                # followed by an admitted INSERT would still race with deletion.
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM pending_service_outputs WHERE root=? AND kind=?',(row['root'],row['kind'])).fetchone()
                if not _same_pending(current,row):
                    continue
                if temporary is not None:
                    if registered != [r[0] for r in db.execute('SELECT local FROM temporary_files WHERE root=? AND purpose=? ORDER BY local',(row['root'],purpose))]:
                        continue
                    _finish_pair(target,temporary,signature,checkpoint)
                    db.execute('DELETE FROM temporary_files WHERE root=? AND local=?',(row['root'],os.path.basename(temporary)))
                elif _signature(target,row['sha256']) != signature:
                    continue
                checkpoint()
                db.execute('INSERT OR REPLACE INTO service_outputs VALUES(?,?,?,?)',tuple(row[key] for key in ('root','kind','local','sha256')))
                db.execute('DELETE FROM pending_service_outputs WHERE root=? AND kind=?',(row['root'],row['kind']))
            remember(store,row['root'],row['kind'],row['local'],target,row['sha256'],checkpoint)
        except InterruptedError:
            raise
        except (OSError,ValueError):
            continue
    for row in store.rows('SELECT p.*,j.root FROM pending_publications p JOIN jobs j ON j.id=p.job'):
        checkpoint()
        progress(row['local'])
        try:
            target = safe_join(row['root'], row['local'])
            signature = _signature(target,row['sha256'])
            if signature[-1] not in (1, 2) or signature[2] != row['size']:
                continue
            key = root_key(row['root'])
            temporary = None
            if signature[-1] == 2:
                with store.db() as db:
                    owned = _media_owner(db,row,key)
                if not owned:
                    continue
                temporary = safe_join(row['root'],row['temp'])
                if not _pair_matches(target,temporary,signature):
                    continue
            signature = _verified(target,row['sha256'],checkpoint,row['size'],before=signature)
            if signature is None:
                continue
            checkpoint()
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT p.*,j.root FROM pending_publications p JOIN jobs j ON j.id=p.job WHERE p.job=? AND p.path=? AND p.type=?',(row['job'],row['path'],row['type'])).fetchone()
                if not _same_pending(current,row):
                    continue
                if temporary is not None:
                    if not _media_owner(db,row,key):
                        continue
                    _finish_pair(target,temporary,signature,checkpoint)
                    db.execute('DELETE FROM temporary_files WHERE root=? AND local=?',(key,row['temp']))
                elif _signature(target,row['sha256']) != signature:
                    continue
                elif _media_owner(db,row,key) and not os.path.lexists(safe_join(row['root'],row['temp'])):
                    # A previous recovery may have removed the second name and
                    # then been cancelled before committing the artifact.
                    db.execute('DELETE FROM temporary_files WHERE root=? AND local=?',(key,row['temp']))
                checkpoint()
                db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)', tuple(row[k] for k in ('job','path','type','local','size','sha256','fingerprint','preview_size')))
                db.execute('DELETE FROM partials WHERE job=? AND path=? AND type=?', (row['job'],row['path'],row['type']))
                db.execute('DELETE FROM partial_ownership WHERE job=? AND local=?', (row['job'],row['temp']))
                db.execute('DELETE FROM pending_publications WHERE job=? AND path=? AND type=?', (row['job'],row['path'],row['type']))
            recovered += 1
        except InterruptedError:
            raise
        except (OSError, ValueError):
            continue
    return recovered

class Recovery:
    """Expose crash recovery immediately without serving conflicting commands."""
    def __init__(self, store):
        self.store=store
        self.lock=threading.Lock()
        self.cancel=threading.Event()
        self.thread=None
        self.result={'busy':False,'error':'','processed':0,'total':0,'current':'','recovered':0}

    def snapshot(self):
        with self.lock: return dict(self.result)

    def start(self):
        with self.lock:
            if self.result['busy']: return
            with self.store.db() as db:
                total=db.execute('SELECT (SELECT COUNT(*) FROM pending_publications)+(SELECT COUNT(*) FROM pending_service_outputs)+(SELECT COUNT(*) FROM duplicate_moves)').fetchone()[0]
            self.result.update(busy=bool(total),error='',processed=0,total=total,current='')
            if not total: return
            self.cancel.clear()
            self.thread=threading.Thread(target=self.run,name='disk-recovery',daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                self.result.update(busy=False,error='На ПК не удалось запустить восстановление. Закройте лишние программы и нажмите «Повторить восстановление».')

    def run(self):
        def checkpoint():
            if self.cancel.is_set(): raise InterruptedError('Восстановление остановлено')
        def progress(path):
            with self.lock:
                self.result['processed']+=1
                self.result['current']=path
        try:
            recovered=recover_publications(self.store,checkpoint,progress)
            with self.lock: self.result['recovered']=recovered
        except Exception:
            with self.lock: self.result['error']='Приложение на ПК не смогло завершить восстановление. Проверьте подключение диска и свободное место, затем нажмите «Повторить восстановление».'
        finally:
            with self.lock: self.result['busy']=False
