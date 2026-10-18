"""Remove an owned preview only after its original has been verified."""
import hashlib
import os
import stat
import uuid
from pathlib import Path

from .paths import safe_join, publish


def remove_owned(root, record, checkpoint=lambda: None):
    filename = safe_join(root, record['local'])
    if not os.path.lexists(filename):
        return True
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
        create.restype = wintypes.HANDLE
        # Deny writers/deleters until validation and deletion have completed.
        handle = create(str(filename), 0x80010000, 1, None, 3, 0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return False
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != record['size']:
                return False
            sha = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                checkpoint(); sha.update(chunk)
            if sha.hexdigest().lower() != record['sha256'].lower():
                return False
            checkpoint()
            delete = kernel.SetFileInformationByHandle
            delete.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            delete.restype = wintypes.BOOL
            flag = wintypes.BOOL(True)
            return bool(delete(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)))
    # Quarantine first so a replacement at the original path is never unlinked.
    preserved = Path(safe_join(root, '.yadisk-preserved-preview-' + uuid.uuid4().hex))
    os.rename(filename, preserved)
    removed = False
    try:
        info = os.lstat(preserved)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != record['size']:
            return False
        sha = hashlib.sha256()
        with open(preserved, 'rb') as stream:
            while chunk := stream.read(1024 * 1024):
                checkpoint(); sha.update(chunk)
            after = os.fstat(stream.fileno())
            if any(getattr(after, key) != getattr(info, key) for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):
                return False
        if sha.hexdigest().lower() != record['sha256'].lower():
            return False
        checkpoint()
        preserved.unlink(); removed = True
        return True
    finally:
        if not removed and os.path.lexists(preserved):
            try:
                publish(preserved, filename)
            except OSError:
                # Preserve content under the quarantine name if the path is busy.
                pass
