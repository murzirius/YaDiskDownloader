"""Keep native text at the monitor's resolution, including COM folder dialogs."""
import os
from contextlib import contextmanager


def enable():
    if os.name == 'nt':
        import ctypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        try:
            user.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass
        try:
            user.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass


@contextmanager
def native_thread():
    previous = None
    if os.name == 'nt':
        import ctypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        try:
            user.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass
    try:
        yield
    finally:
        if previous:
            user.SetThreadDpiAwarenessContext(previous)
