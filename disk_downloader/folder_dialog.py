"""Native Windows folder selection, independent of Tcl/Tk in frozen builds."""
import os
import threading
import uuid


def choose_directory(initial=""):
    if os.name != "nt":
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            try:
                return filedialog.askdirectory(parent=root, initialdir=initial or None, title="Папка для результатов Яндекс.Диска")
            finally:
                root.destroy()
        except ImportError as exc:
            raise OSError("В системе нет диалога выбора папки. Введите путь в поле вручную.") from exc
    import ctypes as c
    from ctypes import wintypes as w

    class GUID(c.Structure):
        _fields_ = [("bytes", c.c_ubyte * 16)]
        def __init__(self, value):
            super().__init__((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))

    ole = c.OleDLL("ole32")
    ole.CoInitializeEx.argtypes = [c.c_void_p, w.DWORD]
    ole.CoCreateInstance.argtypes = [c.POINTER(GUID), c.c_void_p, w.DWORD, c.POINTER(GUID), c.POINTER(c.c_void_p)]
    ole.CoTaskMemFree.argtypes = [c.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    dialog, item = c.c_void_p(), c.c_void_p()

    def method(obj, index, *arguments):
        table = c.cast(obj, c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(c.c_long, c.c_void_p, *arguments)(table[index])

    ole.CoInitializeEx(None, 6)  # Dedicated worker is always an STA.
    try:
        ole.CoCreateInstance(c.byref(GUID("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")), None, 1,
                             c.byref(GUID("d57c7288-d4ad-4768-be02-9d969532d960")), c.byref(dialog))
        method(dialog, 9, w.DWORD)(dialog, 0x20 | 0x40 | 0x800 | 0x8 | 0x2000000)
        method(dialog, 17, w.LPCWSTR)(dialog, "Папка для результатов Яндекс.Диска")
        if initial and os.path.isdir(initial):
            shell = c.OleDLL("shell32")
            shell.SHCreateItemFromParsingName.argtypes = [w.LPCWSTR, c.c_void_p, c.POINTER(GUID), c.POINTER(c.c_void_p)]
            initial_item = c.c_void_p()
            try:
                shell.SHCreateItemFromParsingName(initial, None, c.byref(GUID("43826d1e-e718-42ee-bc55-a1e261c37bfe")), c.byref(initial_item))
                method(dialog, 12, c.c_void_p)(dialog, initial_item)
            except OSError:
                pass
            finally:
                if initial_item:
                    method(initial_item, 2)(initial_item)
        result = method(dialog, 3, w.HWND)(dialog, None)
        if result & 0xffffffff == 0x800704c7:
            return ""
        if result < 0:
            raise OSError(f"Windows не открыла выбор папки (HRESULT 0x{result & 0xffffffff:08X}).")
        result = method(dialog, 20, c.POINTER(c.c_void_p))(dialog, c.byref(item))
        if result < 0 or not item:
            raise OSError("Windows не вернула выбранную папку.")
        value = c.c_void_p()
        result = method(item, 5, w.DWORD, c.POINTER(c.c_void_p))(item, 0x80058000, c.byref(value))
        try:
            if result < 0 or not value:
                raise OSError("Не удалось прочитать путь выбранной папки.")
            return c.wstring_at(value)
        finally:
            if value:
                ole.CoTaskMemFree(value)
    finally:
        if item:
            method(item, 2)(item)
        if dialog:
            method(dialog, 2)(dialog)
        ole.CoUninitialize()


class FolderPicker:
    def __init__(self, chooser=None):
        self.chooser = chooser or choose_directory
        self.lock = threading.Lock()
        self.result = {"busy": False, "path": "", "error": "", "request_id": ""}
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.result)

    def start(self, initial=""):
        if not isinstance(initial, str) or len(initial) > 32760 or "\0" in initial:
            raise ValueError("Некорректный начальный путь папки. Введите обычный путь Windows.")
        with self.lock:
            if self.result["busy"]:
                return dict(self.result)
            self.result = {"busy": True, "path": "", "error": "", "request_id": uuid.uuid4().hex}
            result = dict(self.result)
            self.thread = threading.Thread(target=self.run, args=(initial,), name="disk-folder-dialog", daemon=True)
            self.thread.start()
        return result

    def run(self, initial):
        try:
            path = self.chooser(initial)
            with self.lock:
                self.result.update(path=path or "")
        except Exception as exc:
            code = getattr(exc, "winerror", None) or getattr(exc, "hresult", None)
            with self.lock:
                self.result["error"] = "Не удалось открыть окно выбора папки Windows" + (f" (код {code})" if code else "") + ". Введите или вставьте полный путь в поле папки. Скачивание не запускается от открытия диалога."
        finally:
            with self.lock:
                self.result["busy"] = False
