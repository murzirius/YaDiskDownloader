"""Exercise 100/150/200% DPI messages without changing Windows display settings.

This verifies native layout and font lifecycle, not physical multi-monitor moves.
"""
import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes as w
from pathlib import Path


def check(output):
    from disk_downloader import desktop
    results=[]
    def inspect(window):
        user=window.user;gdi=window.gdi
        user.ShowWindow(window.hwnd,0)
        # Synthetic DPI can request a window larger than the CI desktop.
        # An overlapped window is clamped by Windows before layout is checked.
        # Isolate this geometry test from the host monitor using a hidden popup;
        # retain the production controls, scaling handler and every assertion.
        user.GetWindowLongPtrW.argtypes=(w.HWND,ctypes.c_int)
        user.GetWindowLongPtrW.restype=ctypes.c_ssize_t
        user.SetWindowLongPtrW.argtypes=(w.HWND,ctypes.c_int,ctypes.c_ssize_t)
        user.SetWindowLongPtrW.restype=ctypes.c_ssize_t
        style=user.GetWindowLongPtrW(window.hwnd,-16)
        user.SetWindowLongPtrW(window.hwnd,-16,(style&~0x00cf0000)|0x80000000)
        user.GetClientRect.argtypes=(w.HWND,ctypes.POINTER(w.RECT))
        user.GetWindowRect.argtypes=(w.HWND,ctypes.POINTER(w.RECT))
        user.ClientToScreen.argtypes=(w.HWND,ctypes.POINTER(w.POINT))
        gdi.GetObjectW.argtypes=(w.HANDLE,ctypes.c_int,ctypes.c_void_p)
        kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=w.HANDLE
        user.GetGuiResources.argtypes=(w.HANDLE,w.DWORD)
        process=kernel.GetCurrentProcess()
        before=user.GetGuiResources(process,0)
        for dpi in (96,144,192,144,96)*4+(384,96):
            rectangle=w.RECT(0,0,round(616*dpi/96),round(459*dpi/96))
            user.SendMessageW(window.hwnd,0x02e0,dpi|(dpi<<16),ctypes.addressof(rectangle))
            assert window.dpi==dpi
            client=w.RECT();user.GetClientRect(window.hwnd,ctypes.byref(client))
            origin=w.POINT(0,0);user.ClientToScreen(window.hwnd,ctypes.byref(origin))
            for key,handle in window.controls.items():
                rect=w.RECT();user.GetWindowRect(handle,ctypes.byref(rect))
                assert rect.left>=origin.x and rect.top>=origin.y,(dpi,key)
                assert rect.right<=origin.x+client.right and rect.bottom<=origin.y+client.bottom,(dpi,key)
            font=ctypes.create_string_buffer(128)
            assert gdi.GetObjectW(window.font,len(font),font)>0
            height=ctypes.cast(font,ctypes.POINTER(ctypes.c_long)).contents.value
            assert height==-round(16*dpi/96),(dpi,height)
            results.append({'dpi':dpi,'font_height':height,'all_controls_fit':True})
        assert user.GetGuiResources(process,0)<=before+1,'Repeated scaling leaked GDI objects'
    desktop.launch(lambda:None,inspect_window=inspect)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'simulation':True,'cases':results,'gdi_resources_stable':True},indent=2),encoding='utf8')


if __name__=='__main__':
    if os.name!='nt':raise SystemExit('Windows only')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('desktop-dpi-results.json'))
    check(parser.parse_args().output)
