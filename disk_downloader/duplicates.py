"""Content-based reuse. Names, dates and EXIF never authorize discarding data."""
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .paths import file_signature, safe_join, publish


@contextmanager
def content_lock(job, kind, meta, control):
    key=meta.get('sha256','')
    enabled=job['settings'].get('deduplicate') and re.fullmatch('[a-fA-F0-9]{64}',key or '')
    lock=control.duplicate_locks[int(key[:8],16)%len(control.duplicate_locks)] if enabled else None
    if lock:
        while not lock.acquire(timeout=.15):control.check()
    try:
        control.check()
        yield
    finally:
        if lock:lock.release()


@contextmanager
def locked_file(root, record):
    """Deny Windows writers/deleters, including through hardlinks, while hashing."""
    filename=safe_join(root,record['local'])
    if os.name=='nt':
        import ctypes,msvcrt
        from ctypes import wintypes as w
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        create=kernel.CreateFileW
        create.argtypes=(w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.c_void_p);create.restype=w.HANDLE
        handle=create(str(filename),0x80000000,1,None,3,0x00200000,None)
        if handle in (None,ctypes.c_void_p(-1).value):raise OSError('File unavailable')
        try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except BaseException:
            kernel.CloseHandle.argtypes=(w.HANDLE,);kernel.CloseHandle(handle);raise
        stream=os.fdopen(fd,'rb')
    else:stream=open(filename,'rb')
    with stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=record['size']:
            raise OSError('Not a single owned regular file')
        yield stream


def verify_stream(stream,record,check):
    before=os.fstat(stream.fileno());sha=hashlib.sha256()
    while chunk:=stream.read(1024*1024):check();sha.update(chunk)
    after=os.fstat(stream.fileno())
    return sha.hexdigest()==record['sha256'].lower() and all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))


def verified(engine,job,record,control):
    try:
        filename=safe_join(job['root'],record['local']);signature=file_signature(filename,record['sha256'])
        if signature[2]!=record['size']:return False
        with engine.store.db() as db:
            cache=db.execute('SELECT signature FROM verification_cache WHERE job=? AND local=?',(job['id'],record['local'])).fetchone()
        if job['settings'].get('verification_mode','fast')!='full' and signature[-1] is not None and cache and cache[0]==json.dumps(signature):return True
        with locked_file(job['root'],record) as stream:
            if not verify_stream(stream,record,control.check):return False
        if signature!=file_signature(filename,record['sha256']):return False
        engine.remember_file(job,record['local'],record['sha256'])
        return True
    except (OSError,ValueError):return False


def reuse(engine,job,entry,kind,meta,control):
    # Original checksum comes from the current Yandex response. Preview bytes
    # cannot be inferred from EXIF or from the checksum of the original.
    sha=meta.get('sha256','')
    if kind!='original' or not re.fullmatch('[a-fA-F0-9]{64}',sha or '') or not isinstance(meta.get('size'),int):return False
    with engine.store.db() as db:
        rows=db.execute("SELECT * FROM artifacts WHERE job=? AND type='original' AND sha256=? AND size=? AND path!=? GROUP BY local ORDER BY local LIMIT 100",(job['id'],sha.lower(),meta['size'],entry['path'])).fetchall()
    for row in rows:
        control.check();record=dict(row)
        if not verified(engine,job,record,control):continue
        from .engine import fingerprint
        with engine.store.db() as db:
            db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)',(job['id'],entry['path'],kind,record['local'],record['size'],record['sha256'],fingerprint(meta),None))
        control.event('cache','Точная копия уже есть: '+record['local']+'. Повторная загрузка не нужна.',entry['path'],'success')
        return True
    return False


def compact(engine,job,entry,kind,meta,control):
    # Scans only build a review list. Persisted legacy settings cannot delete.
    if not job['settings'].get('deduplicate'):return
    # One cleanup at a time, without holding an SQLite write transaction during IO.
    lock=control.duplicate_cleanup_lock
    while not lock.acquire(timeout=.15):control.check()
    try:
        with engine.store.db() as db:
            current=db.execute('SELECT * FROM artifacts WHERE job=? AND path=? AND type=?',(job['id'],entry['path'],kind)).fetchone()
            if not current:return
            current=dict(current)
            rows=db.execute('SELECT * FROM artifacts WHERE job=? AND type=? AND sha256=? AND size=? AND preview_size IS ? AND local<? GROUP BY local ORDER BY local LIMIT 100',(job['id'],kind,current['sha256'],current['size'],current['preview_size'],current['local'])).fetchall()
        for row in rows:
            control.check();canonical=dict(row)
            if not verified(engine,job,canonical,control):continue
            if not verified(engine,job,current,control):return
            with engine.store.db() as db:
                db.execute('INSERT OR REPLACE INTO duplicate_candidates VALUES(?,?,?,?,?,?)',(job['id'],current['local'],canonical['local'],kind,current['size'],current['sha256']))
            break
    finally:lock.release()


def recycle_duplicate(engine,job,duplicate,canonical,control):
    # Windows' Recycle Bin must be available. On NAS/removable volumes failure
    # preserves the copy; permanent deletion is never a fallback.
    if os.name!='nt':return False
    original=safe_join(job['root'],duplicate['local'])
    quarantine=Path(safe_join(job['root'],'.yadisk-duplicate-'+uuid.uuid4().hex))
    moved=False;completed=False;references=[]
    try:
        with locked_file(job['root'],canonical) as survivor:
            if not verify_stream(survivor,canonical,control.check):return False
            with locked_file(job['root'],duplicate) as stream:
                if not verify_stream(stream,duplicate,control.check):return False
            control.check()
            # Commit all references before moving bytes. A crash can leave an
            # extra copy, never an intentionally deleted last recorded copy.
            with engine.store.db() as db:
                references=db.execute('SELECT path,type,fingerprint FROM artifacts WHERE job=? AND local=? AND type=? AND sha256=? AND size=?',(job['id'],duplicate['local'],duplicate['type'],duplicate['sha256'],duplicate['size'])).fetchall()
                db.execute('INSERT OR REPLACE INTO duplicate_moves VALUES(?,?,?,?,?)',(job['id'],duplicate['local'],quarantine.name,duplicate['sha256'],duplicate['size']))
                db.execute('UPDATE artifacts SET local=? WHERE job=? AND local=? AND type=? AND sha256=? AND size=?',(canonical['local'],job['id'],duplicate['local'],duplicate['type'],duplicate['sha256'],duplicate['size']))
            os.rename(original,quarantine);moved=True
            quarantined=dict(duplicate,local=quarantine.name)
            with locked_file(job['root'],quarantined) as stream:
                if not verify_stream(stream,quarantined,control.check):raise OSError('Duplicate changed')
            from .maintenance import recycle
            recycle(quarantine)
            moved=False;completed=True
        return True
    except OSError:
        control.event('cache','Копия оставлена: корзина недоступна или файл занят. Безвозвратное удаление не используется.',duplicate['path'],'warning')
        return False
    finally:
        if moved and os.path.lexists(quarantine):
            try:publish(quarantine,original)
            except OSError:
                control.event('cache','Копия сохранена в служебном файле '+quarantine.name+'. Проверьте доступ к исходной папке.',duplicate['path'],'warning')

        if not completed and references:
            with engine.store.db() as db:
                db.executemany('UPDATE artifacts SET local=? WHERE job=? AND path=? AND type=? AND fingerprint=? AND local=?',( (duplicate['local'],job['id'],r['path'],r['type'],r['fingerprint'],canonical['local']) for r in references))

        if not os.path.lexists(quarantine):
            with engine.store.db() as db:
                db.execute('DELETE FROM duplicate_moves WHERE job=? AND local=?',(job['id'],duplicate['local']))


def recover_moves(store,checkpoint,progress):
    recovered=0
    for row in store.rows('SELECT d.*,j.root FROM duplicate_moves d JOIN jobs j ON j.id=d.job'):
        checkpoint();progress(row['local'])
        if not re.fullmatch(r'\.yadisk-duplicate-[a-f0-9]{32}',row['quarantine']):
            raise OSError('Invalid duplicate recovery path')
        original=safe_join(row['root'],row['local']);quarantine=safe_join(row['root'],row['quarantine'])
        if os.path.lexists(quarantine):
            if os.path.lexists(original):raise OSError('Recovery path already occupied')
            record=dict(row,local=row['quarantine'])
            with locked_file(row['root'],record) as stream:
                if not verify_stream(stream,record,checkpoint):raise OSError('Recovery file changed')
            publish(quarantine,original);recovered+=1
        with store.db() as db:db.execute('DELETE FROM duplicate_moves WHERE job=? AND local=?',(row['job'],row['local']))
    return recovered
