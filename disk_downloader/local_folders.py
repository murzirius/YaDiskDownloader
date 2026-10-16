"""Read-only folder navigation for the in-app destination picker."""
import os
from pathlib import Path


def listing(value=""):
    if not isinstance(value, str) or len(value) > 32760 or "\0" in value:
        raise ValueError("Введите обычный полный путь к папке на ПК.")
    path = Path(value).expanduser() if value else Path.home()
    if not path.is_absolute():
        raise ValueError("Нужен полный путь, например C:\\Users или D:\\Фото.")
    requested = path
    while not path.is_dir() and path != path.parent:
        path = path.parent
    folders = []
    truncated = False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append({'name':entry.name, 'path':str(path/entry.name)})
                        if len(folders) >= 500:
                            truncated = True
                            break
                except OSError:
                    continue
    except PermissionError as exc:
        raise ValueError("Windows не разрешает открыть эту папку. Выберите другую или проверьте права доступа.") from exc
    except OSError as exc:
        raise ValueError("Папка недоступна. Проверьте, что диск подключён, и выберите другую папку.") from exc
    places = [{'name':'Домашняя папка', 'path':str(Path.home())}]
    for name, folder in [('Загрузки',Path.home()/'Downloads'),('Документы',Path.home()/'Documents')]:
        if folder.is_dir():
            places.append({'name':name, 'path':str(folder)})
    if os.name == 'nt':
        import ctypes
        get_drives = ctypes.windll.kernel32.GetLogicalDrives
        get_drives.restype = ctypes.c_uint32
        drives = get_drives()
        for index in range(26):
            if drives & (1 << index):
                drive = chr(65+index)+':\\'
                places.append({'name':'Диск '+drive[:2], 'path':drive})
    else:
        places.append({'name':'Корень', 'path':'/'})
    return {'path':str(path), 'parent':str(path.parent) if path.parent != path else None,
            'folders':sorted(folders,key=lambda item:item['name'].casefold()), 'places':places,
            'truncated':truncated, 'missing':path != requested}
