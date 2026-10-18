import hashlib
import os
import re
import sqlite3
import tempfile
import time
from functools import lru_cache
from pathlib import Path


class PathError(ValueError):
    pass


def remote_path(value):
    if not isinstance(value, str) or not value.startswith("disk:/") or "\x00" in value:
        raise PathError("Ожидается абсолютный путь disk:/…")
    value = value.rstrip("/") if value != "disk:/" else value
    parts = value[6:].split("/") if value != "disk:/" else []
    if any(p in ("", ".", "..") for p in parts):
        raise PathError("Некорректные компоненты пути Диска")
    return value


def parent_path(value):
    value = remote_path(value)
    return "disk:/" if "/" not in value[6:] else value.rsplit("/", 1)[0]


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def component(name, identity):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")
    reserved = re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", cleaned.split(".")[0])
    changed = cleaned != name or reserved or cleaned in ("", ".", "..")
    if reserved:
        cleaned = "_" + cleaned
    cleaned = cleaned or "unnamed"
    if len(cleaned.encode("utf-16-le")) > 240:
        stem, ext = os.path.splitext(cleaned)
        ext = ext[:20]
        stem = stem[:75]
        while len((stem + ext).encode("utf-16-le")) > 190:
            stem = stem[:-1]
        cleaned = stem + ext
        changed = True
    return suffix(cleaned, digest(identity)) if changed else cleaned


def suffix(name, tag):
    stem, ext = os.path.splitext(name)
    return f"{stem}~{tag}{ext}"


def native(path):
    path = str(Path(path).absolute())
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
    return path


@lru_cache(maxsize=1)
def change_time_api():
    import ctypes
    from ctypes import wintypes
    class BasicInfo(ctypes.Structure):
        _fields_ = [("created", ctypes.c_longlong), ("accessed", ctypes.c_longlong), ("written", ctypes.c_longlong), ("changed", ctypes.c_longlong), ("attributes", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel, BasicInfo


def file_signature(filename, sha):
    info = os.stat(filename)
    changed = info.st_ctime_ns
    if os.name == "nt":
        import ctypes
        kernel, BasicInfo = change_time_api()
        handle = kernel.CreateFileW(str(filename), 128, 7, None, 3, 0, None)
        changed = None
        if handle not in (None, ctypes.c_void_p(-1).value):
            try:
                basic = BasicInfo()
                if kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                    changed = basic.changed
            finally:
                kernel.CloseHandle(handle)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, sha, changed)


def publish(temp, target):
    if os.name == "nt":
        durable_move(temp, target, False)
    else:
        os.link(temp, target)
        os.unlink(temp)


def durable_move(source, target, replace):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if not move(str(source), str(target), 8 | (1 if replace else 0)):
        raise ctypes.WinError(ctypes.get_last_error())


def temporary_file(root, store=None, purpose='temporary'):
    descriptor, filename = tempfile.mkstemp(prefix=".disk-", suffix=".part", dir=safe_join(root, ""))
    os.close(descriptor)
    if store:
        from .service_outputs import root_key
        try:
            with store.db() as db:
                db.execute('INSERT INTO temporary_files VALUES(?,?,?,?)', (root_key(root), Path(filename).name, purpose, time.time()))
        except BaseException:
            try:
                os.unlink(filename)
            except OSError:
                pass
            raise
    return filename


def forget_temporary(store, root, filename):
    """Forget only a vanished owned temp, never discard a resumable part."""
    from .service_outputs import root_key
    if not os.path.lexists(filename):
        try:
            with store.db() as db:
                db.execute('DELETE FROM temporary_files WHERE root=? AND local=?', (root_key(root),Path(filename).name))
        except sqlite3.Error:
            # Registry housekeeping is recoverable; result commits are separate.
            pass


def replace_owned(source, target):
    for attempt in range(4):
        try:
            if os.name == 'nt':
                durable_move(source, target, True)
            else:
                os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 3:
                raise
            time.sleep(0.1 * 2 ** attempt)


def reserve_output(store, job, name):
    candidate = name
    counter = 0
    with store.db() as db:
        while db.execute("SELECT 1 FROM names WHERE job=? AND folded=?", (job["id"], candidate.casefold())).fetchone() or os.path.lexists(safe_join(job["root"], candidate)):
            counter += 1
            candidate = suffix(name, digest(job["id"] + name + str(counter)))
        db.execute("INSERT INTO names VALUES(?,?,?,?)", (job["id"], "@output:" + candidate, candidate, candidate.casefold()))
    return candidate


def safe_join(root, relative):
    if not isinstance(relative, str) or relative.startswith(("/", "\\")):
        raise PathError("Недопустимый локальный путь")
    parts = relative.split("/") if relative else []
    if any(p in ("", ".", "..") or ":" in p or "\\" in p or p.endswith((".", " ")) or any(ord(c) < 32 for c in p) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", p.split(".")[0]) for p in parts):
        raise PathError("Недопустимый локальный путь")
    root = Path(root).absolute()
    candidate = root.joinpath(*parts)
    real_root = root.resolve()
    if not candidate.resolve().is_relative_to(real_root):
        raise PathError("Путь выходит за пределы папки назначения")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise PathError("Символьные ссылки и junction в папке назначения запрещены")
    if len(str(candidate)) > 30000:
        raise PathError("Путь превышает предел файловой системы")
    return native(candidate)


def normalize_selection(paths, whole):
    if whole:
        return ["disk:/"]
    paths = sorted(set(remote_path(p) for p in paths), key=lambda p: (p.count("/"), p))
    if not paths:
        raise ValueError("Выберите файлы, папки или весь Диск")
    result = []
    accepted = set()
    for path in paths:
        ancestor = path
        while ancestor not in accepted and ancestor != "disk:/":
            ancestor = parent_path(ancestor)
        if ancestor not in accepted:
            result.append(path)
            accepted.add(path)
    return result
