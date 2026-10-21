"""Compact native Windows status window. All filesystem/network work stays off its UI thread."""
import os
import queue
import threading

_window = None


def progress(text, percent):
    if _window:
        _window.messages.put(('progress',(text,percent)))


def ready(app, url, shutdown):
    if _window:
        _window.app=app;_window.url=url;_window.shutdown=shutdown
        if _window.closing:shutdown()
        else:_window.messages.put(('ready',None))


def show():
    if _window and _window.hwnd and not _window.closing:
        _window.user.PostMessageW(_window.hwnd,0x8001,0,0)
        return True
    return False


def status():
    return {'enabled':_window is not None,'ready':bool(_window and _window.app),'closing':bool(_window and _window.closing)}


def launch(main, inspect_window=None):
    import ctypes
    from ctypes import wintypes as w
    from .windows_dpi import enable
    enable()
    global _window
    class Window:
        def __init__(self):
            self.user=ctypes.WinDLL('user32',use_last_error=True)
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.user.CreateWindowExW.restype=w.HWND
            self.user.CreateWindowExW.argtypes=(w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,ctypes.c_void_p)
            self.user.DefWindowProcW.argtypes=(w.HWND,w.UINT,w.WPARAM,w.LPARAM);self.user.DefWindowProcW.restype=ctypes.c_ssize_t
            self.user.SendMessageW.argtypes=(w.HWND,w.UINT,w.WPARAM,w.LPARAM);self.user.SendMessageW.restype=ctypes.c_ssize_t
            self.user.PostMessageW.argtypes=(w.HWND,w.UINT,w.WPARAM,w.LPARAM)
            self.user.SetWindowTextW.argtypes=(w.HWND,w.LPCWSTR)
            self.user.GetWindowTextW.argtypes=(w.HWND,w.LPWSTR,ctypes.c_int)
            self.user.EnableWindow.argtypes=(w.HWND,w.BOOL)
            self.user.DestroyWindow.argtypes=(w.HWND,)
            self.user.ShowWindow.argtypes=(w.HWND,ctypes.c_int)
            self.user.SetForegroundWindow.argtypes=(w.HWND,)
            self.user.IsIconic.argtypes=(w.HWND,);self.user.IsIconic.restype=w.BOOL
            self.user.SetTimer.argtypes=(w.HWND,ctypes.c_size_t,w.UINT,ctypes.c_void_p)
            self.kernel.GetModuleHandleW.argtypes=(w.LPCWSTR,);self.kernel.GetModuleHandleW.restype=w.HMODULE
            self.messages=queue.Queue();self.app=None;self.url=None;self.shutdown=None;self.closing=False
            self.exit_code=0
            self.snapshot_busy=False;self.job=None;self.jobs=[];self.tick=0;self.controls={};self.positions={}
            self.user.GetDpiForSystem.restype=w.UINT
            self.user.GetDpiForWindow.argtypes=(w.HWND,);self.user.GetDpiForWindow.restype=w.UINT
            self.dpi=self.user.GetDpiForSystem() or 96
            self.user.MoveWindow.argtypes=(w.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.BOOL)
            self.user.SetWindowPos.argtypes=(w.HWND,w.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.UINT)
            self.instance=self.kernel.GetModuleHandleW(None)
            self.user.LoadImageW.argtypes=(w.HINSTANCE,ctypes.c_void_p,w.UINT,ctypes.c_int,ctypes.c_int,w.UINT)
            self.user.LoadImageW.restype=w.HANDLE
            self.small_icon=None
            self.icon=self.user.LoadImageW(self.instance,ctypes.c_void_p(1),1,self.px(32),self.px(32),0)
            self.gdi=ctypes.WinDLL('gdi32')
            self.gdi.CreateFontW.argtypes=(ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.LPCWSTR)
            self.gdi.CreateFontW.restype=w.HANDLE
            self.font=self.gdi.CreateFontW(-self.px(16),0,0,0,400,0,0,0,1,0,0,5,0,'Segoe UI')
            self.heading_font=self.gdi.CreateFontW(-self.px(26),0,0,0,600,0,0,0,1,0,0,5,0,'Segoe UI')
            self.label_font=self.gdi.CreateFontW(-self.px(13),0,0,0,600,0,0,0,1,0,0,5,0,'Segoe UI')
            self.gdi.CreateSolidBrush.argtypes=(w.DWORD,);self.gdi.CreateSolidBrush.restype=w.HBRUSH
            self.gdi.SetTextColor.argtypes=(w.HDC,w.DWORD)
            self.gdi.SetBkColor.argtypes=(w.HDC,w.DWORD)
            self.gdi.SelectObject.argtypes=(w.HDC,w.HANDLE);self.gdi.SelectObject.restype=w.HANDLE
            self.gdi.RoundRect.argtypes=(w.HDC,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int)
            self.user.FillRect.argtypes=(w.HDC,ctypes.POINTER(w.RECT),w.HBRUSH)
            self.user.DrawTextW.argtypes=(w.HDC,w.LPCWSTR,ctypes.c_int,ctypes.POINTER(w.RECT),w.UINT)
            self.user.InvalidateRect.argtypes=(w.HWND,ctypes.c_void_p,w.BOOL)
            self.background=self.gdi.CreateSolidBrush(0x1b1715)
            self.panel=self.gdi.CreateSolidBrush(0x241f1c)
            self.input_brush=self.gdi.CreateSolidBrush(0x342b25)
            self.button_brush=self.gdi.CreateSolidBrush(0x493a2d)
            self.accent_brush=self.gdi.CreateSolidBrush(0xe1c3a8)
            self.focus_brush=self.gdi.CreateSolidBrush(0x7e6652)
            self.dark_text=0x281f18;self.light_text=0xeae5e2;self.muted_text=0xbeafa6
            self.paint_resources=[self.font,self.heading_font,self.label_font,self.background,self.panel,self.input_brush,self.button_brush,self.accent_brush,self.focus_brush]
            self.gdi.SetBkMode.argtypes=(w.HDC,ctypes.c_int)
            self.gdi.GetStockObject.restype=w.HANDLE
            self.callback=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)(self.wndproc)
            class WNDCLASS(ctypes.Structure):
                _fields_=[('style',w.UINT),('proc',ctypes.c_void_p),('cls_extra',ctypes.c_int),('wnd_extra',ctypes.c_int),('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('background',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
            self.wc=WNDCLASS(0,ctypes.cast(self.callback,ctypes.c_void_p),0,0,self.instance,None,None,self.background,None,'YaDiskDownloaderDesktop')
            self.user.RegisterClassW.argtypes=(ctypes.POINTER(WNDCLASS),)
            if not self.user.RegisterClassW(ctypes.byref(self.wc)):raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd=self.user.CreateWindowExW(0,self.wc.name,'YaDiskDownloader — состояние',0x02CA0000,160,100,self.px(616),self.px(459),None,None,self.instance,None)
            if not self.hwnd:raise ctypes.WinError(ctypes.get_last_error())
            try:
                dark=w.BOOL(True);dwm=ctypes.WinDLL('dwmapi')
                dwm.DwmSetWindowAttribute.argtypes=(w.HWND,w.DWORD,ctypes.c_void_p,w.DWORD)
                dwm.DwmSetWindowAttribute(self.hwnd,20,ctypes.byref(dark),ctypes.sizeof(dark))
            except OSError:pass
            ctypes.WinDLL('comctl32').InitCommonControls()
            self.control('STATIC','YaDiskDownloader',28,22,544,38,1)
            self.user.SendMessageW(self.controls[1],0x0030,self.heading_font,1)
            self.control('STATIC','СОСТОЯНИЕ ПРИЛОЖЕНИЯ',29,66,542,20,20)
            self.control('STATIC','ЗАПУСК',44,120,512,22,10,0x4000)
            self.control('STATIC','Подготавливаем приложение…',44,151,512,48,2)
            self.control('msctls_progress32','',44,212,512,8,3)
            self.control('STATIC','История и файлы остаются на устройстве.',44,240,512,42,5)
            self.control('STATIC','Задания, подключение Диска и настройки — в браузере.\nЧтобы оставить загрузку работать, сверните это окно.',28,309,544,42,9)
            self.control('BUTTON','Открыть в браузере',28,368,252,38,16)
            self.control('BUTTON','Свернуть',292,368,134,38,18)
            self.control('BUTTON','Выход',438,368,134,38,19)
            for key in (20,10):self.user.SendMessageW(self.controls[key],0x0030,self.label_font,1)
            self.user.EnableWindow(self.controls[16],False)
            self.rescale(self.user.GetDpiForWindow(self.hwnd) or self.dpi)
            self.user.SetTimer(self.hwnd,1,500,None)
            self.user.ShowWindow(self.hwnd,5)

        def px(self,value):
            return round(value*self.dpi/96)

        def rescale(self,dpi):
            if dpi != self.dpi:
                self.dpi=dpi
                old_fonts=(self.font,self.heading_font,self.label_font)
                self.font=self.gdi.CreateFontW(-self.px(16),0,0,0,400,0,0,0,1,0,0,5,0,'Segoe UI')
                self.heading_font=self.gdi.CreateFontW(-self.px(26),0,0,0,600,0,0,0,1,0,0,5,0,'Segoe UI')
                self.label_font=self.gdi.CreateFontW(-self.px(13),0,0,0,600,0,0,0,1,0,0,5,0,'Segoe UI')
                for key,handle in self.controls.items():
                    font=self.heading_font if key==1 else self.label_font if key in (20,10) else self.font
                    self.user.SendMessageW(handle,0x0030,font,1)
                self.gdi.DeleteObject.argtypes=(w.HANDLE,)
                for font in old_fonts:
                    self.gdi.DeleteObject(font);self.paint_resources.remove(font)
                self.paint_resources.extend((self.font,self.heading_font,self.label_font))
            icons=(self.icon,self.small_icon)
            self.icon=self.user.LoadImageW(self.instance,ctypes.c_void_p(1),1,self.px(32),self.px(32),0)
            self.small_icon=self.user.LoadImageW(self.instance,ctypes.c_void_p(1),1,self.px(16),self.px(16),0)
            self.user.SendMessageW(self.hwnd,0x0080,1,self.icon or 0)
            self.user.SendMessageW(self.hwnd,0x0080,0,self.small_icon or 0)
            self.user.DestroyIcon.argtypes=(w.HICON,)
            for icon in icons:
                if icon:self.user.DestroyIcon(icon)
            for key,position in self.positions.items():
                self.user.MoveWindow(self.controls[key],*(self.px(v) for v in position),True)
            self.user.InvalidateRect(self.hwnd,None,True)

        def control(self,kind,text,x,y,width,height,key,extra=0):
            self.positions[key]=(x,y,width,height)
            handle=self.user.CreateWindowExW(0,kind,text,0x50000000|extra|(0x0000000b if kind=='BUTTON' else 0)|(0x00010000 if kind in ('BUTTON','COMBOBOX','EDIT') else 0),self.px(x),self.px(y),self.px(width),self.px(height),self.hwnd,key,self.instance,None)
            self.controls[key]=handle
            self.user.SendMessageW(handle,0x0030,self.font,1)
            if kind in ('COMBOBOX','EDIT','msctls_progress32'):
                theme=ctypes.WinDLL('uxtheme')
                theme.SetWindowTheme.argtypes=(w.HWND,w.LPCWSTR,w.LPCWSTR)
                theme.SetWindowTheme(handle,'','')
            if kind=='msctls_progress32':
                self.user.SendMessageW(handle,0x2001,0,0x342b25)
                self.user.SendMessageW(handle,0x0409,0,0xe1c3a8)

        def text(self,key,text):
            handle=self.controls[key]
            buffer=ctypes.create_unicode_buffer(4097)
            self.user.GetWindowTextW(handle,buffer,len(buffer))
            if buffer.value!=str(text):
                self.user.SetWindowTextW(handle,str(text))
                self.user.InvalidateRect(handle,None,True)

        def draw_button(self,lp):
            class DRAWITEM(ctypes.Structure):
                _fields_=[('type',w.UINT),('id',w.UINT),('item_id',w.UINT),('action',w.UINT),('state',w.UINT),('hwnd',w.HWND),('dc',w.HDC),('rect',w.RECT),('data',ctypes.c_size_t)]
            item=ctypes.cast(lp,ctypes.POINTER(DRAWITEM)).contents
            disabled=bool(item.state&4);primary=item.id==16
            brush=self.accent_brush if primary and not disabled else self.focus_brush if item.state&1 else self.button_brush
            self.user.FillRect(item.dc,ctypes.byref(item.rect),self.background)
            old=self.gdi.SelectObject(item.dc,brush)
            pen=self.gdi.SelectObject(item.dc,self.gdi.GetStockObject(8))
            r=item.rect;self.gdi.RoundRect(item.dc,r.left,r.top,r.right,r.bottom,self.px(12),self.px(12))
            self.gdi.SelectObject(item.dc,pen);self.gdi.SelectObject(item.dc,old)
            old=self.gdi.SelectObject(item.dc,self.font);self.gdi.SetBkMode(item.dc,1)
            self.gdi.SetTextColor(item.dc,self.muted_text if disabled else self.dark_text if primary else self.light_text)
            value=ctypes.create_unicode_buffer(120);self.user.GetWindowTextW(item.hwnd,value,len(value))
            self.user.DrawTextW(item.dc,value.value,-1,ctypes.byref(item.rect),0x25)
            if item.state&0x10:
                focus=w.RECT(r.left+4,r.top+4,r.right-4,r.bottom-4)
                self.user.DrawFocusRect.argtypes=(w.HDC,ctypes.POINTER(w.RECT))
                self.user.DrawFocusRect(item.dc,ctypes.byref(focus))
            self.gdi.SelectObject(item.dc,old)
            return 1

        def paint(self,hwnd):
            class PAINTSTRUCT(ctypes.Structure):
                _fields_=[('dc',w.HDC),('erase',w.BOOL),('rect',w.RECT),('restore',w.BOOL),('update',w.BOOL),('reserved',ctypes.c_byte*32)]
            self.user.BeginPaint.argtypes=(w.HWND,ctypes.POINTER(PAINTSTRUCT));self.user.BeginPaint.restype=w.HDC
            self.user.EndPaint.argtypes=(w.HWND,ctypes.POINTER(PAINTSTRUCT))
            paint=PAINTSTRUCT();dc=self.user.BeginPaint(hwnd,ctypes.byref(paint))
            old=self.gdi.SelectObject(dc,self.panel);pen=self.gdi.SelectObject(dc,self.gdi.GetStockObject(8))
            self.gdi.RoundRect(dc,self.px(24),self.px(102),self.px(576),self.px(290),self.px(18),self.px(18))
            self.gdi.SelectObject(dc,pen);self.gdi.SelectObject(dc,old);self.user.EndPaint(hwnd,ctypes.byref(paint))

        def close(self):
            if self.closing:return
            self.closing=True
            if self.shutdown:self.shutdown()
            self.user.DestroyWindow(self.hwnd)

        def refresh(self,force=False):
            if self.snapshot_busy or not self.app or self.closing:return
            engine=self.app.extensions['engine']
            active=next((job for job,thread in tuple(engine.threads.items()) if thread.is_alive()),None)
            identities=tuple(engine.threads)
            seen=getattr(self,'seen_jobs',())
            newest=next((identity for identity in reversed(identities) if identity not in seen),None)
            self.seen_jobs=identities
            if newest:force=True
            if not force and not active and not self.job:return
            if not force and not active and self.job and self.job.get('_settled'):return
            self.snapshot_busy=True;chosen=newest or (self.job['id'] if self.job else active)
            def work():
                try:
                    if force or not self.jobs:
                        jobs=list(engine.store.rows('SELECT id,title,phase FROM jobs ORDER BY started DESC LIMIT 1'))
                    else:jobs=self.jobs
                    job_id=active or chosen or (jobs[0]['id'] if jobs else None)
                    snapshot=engine.status_cache.snapshot(engine.store.job(job_id)) if job_id else None
                    self.messages.put(('snapshot',(jobs,snapshot)))
                except Exception:
                    self.messages.put(('notice','Приложение на устройстве не смогло получить состояние задания. Откройте интерфейс в браузере.'))
                finally:self.messages.put(('snapshot_done',None))
            try:threading.Thread(target=work,daemon=True,name='disk-desktop-status').start()
            except RuntimeError:
                self.snapshot_busy=False
                self.text(5,'Не удалось начать обновление состояния. Откройте интерфейс в браузере.')

        def drain(self):
            while True:
                try:kind,value=self.messages.get_nowait()
                except queue.Empty:break
                if kind=='progress':self.text(2,value[0]);self.user.SendMessageW(self.controls[3],0x0402,value[1],0)
                elif kind=='notice':self.text(5,value)
                elif kind=='snapshot_done':self.snapshot_busy=False
                elif kind=='ready':
                    self.text(10,'ГОТОВО К РАБОТЕ');self.text(2,'Откройте браузер, чтобы начать загрузку.');self.user.SendMessageW(self.controls[3],0x0402,100,0)
                    self.user.EnableWindow(self.controls[16],True)
                    self.refresh(True)
                elif kind=='snapshot':
                    jobs,job=value;self.jobs=jobs;self.job=job
                    if job:
                        title=job.get('title') or 'Текущее задание'
                        self.text(10,title[:64])
                        job['_settled']=not job.get('live') and not job.get('statistics_pending')
                        from .journal import PHASES
                        action=job.get('active',[{}]);detail=action[0].get('action','') if action else ''
                        self.text(2,(PHASES.get(job['phase'],job['phase']))+(' · '+detail if detail else ''))
                        counts='Уточняем число файлов…' if job.get('statistics_pending') else f"Файлов обработано: {job.get('processed',0)} / {job.get('files',0)}. Оригиналы: {job.get('originals',0)}. Превью: {job.get('previews',0)}."
                        current=action[0] if action else {}
                        percent=int(job.get('percent') or 0)
                        if current.get('unit') in ('records','objects','files','local_files') and current.get('size') is not None:
                            counts=f"{'Записей истории' if current['unit']=='records' else 'Файлов' if current['unit'] in ('files','local_files') else 'Объектов'}: {current['done']} / {current['size']}"
                            percent=int(min(100,current['done']*100/max(1,current['size'])))
                        self.text(5,job.get('pause_reason') or job.get('health',{}).get('warning') or job.get('error') or job.get('network_warning') or counts)
                        self.user.SendMessageW(self.controls[3],0x0402,percent,0)
                    else:
                        self.text(5,'Заданий пока нет. Начните загрузку в браузере.')
                elif kind=='finished':
                    self.closing=True;self.user.DestroyWindow(self.hwnd)
                elif kind=='failed':
                    self.text(2,'Не удалось запустить приложение.');self.text(5,value)
            self.tick+=1
            if self.tick%4==0 and not self.user.IsIconic(self.hwnd):self.refresh()

        def wndproc(self,hwnd,message,wp,lp):
            try:
                if message==0x02e0:
                    rectangle=ctypes.cast(lp,ctypes.POINTER(w.RECT)).contents
                    self.rescale(wp&0xffff)
                    self.user.SetWindowPos(hwnd,None,rectangle.left,rectangle.top,rectangle.right-rectangle.left,rectangle.bottom-rectangle.top,0x0014)
                    return 0
                if message==0x0010:self.close();return 0
                if message==0x0002:
                    self.user.PostQuitMessage(0)
                    return 0
                if message==0x000f:self.paint(hwnd);return 0
                if message==0x002b:return self.draw_button(lp)
                if message in (0x0133,0x0134,0x0135,0x0138):
                    self.gdi.SetBkMode(wp,1);self.gdi.SetTextColor(wp,self.light_text);self.gdi.SetBkColor(wp,0x342b25)
                    if message in (0x0133,0x0134):return self.input_brush
                    key=next((k for k,h in getattr(self,'controls',{}).items() if h==lp),None)
                    if key in (9,20,10):self.gdi.SetTextColor(wp,self.muted_text)
                    return self.background if key in (1,20,9) else self.panel
                if message==0x8001:self.user.ShowWindow(hwnd,9);self.user.SetForegroundWindow(hwnd);self.refresh(True);return 0
                if message==0x0113:self.drain();return 0
                if message==0x0111:
                    key=wp&0xffff
                    if key==16:
                        import webbrowser
                        if self.url:webbrowser.open(self.url)
                    elif key==18:self.user.ShowWindow(hwnd,6)
                    elif key==19:self.close()
                    return 0
            except Exception:
                if hasattr(self,'controls'):self.text(5,'Приложение на устройстве не выполнило команду. Откройте интерфейс в браузере.')
            return self.user.DefWindowProcW(hwnd,message,wp,lp)
    window=Window();_window=window
    if inspect_window:
        # Native layout regression checks run on the owning UI thread.
        inspect_window(window)
    # The native window is already visible before importing Flask/SQLite or migrating.
    def startup():
        try:
            window.exit_code=int(main() or 0)
            window.messages.put(('finished',None))
        except BaseException:
            window.exit_code=1
            if not window.closing:window.messages.put(('failed','Проверьте доступ к папке состояния, свободное место и отсутствие второй копии приложения.'))
    worker=threading.Thread(target=startup,name='disk-desktop-server',daemon=True);worker.start()
    msg=w.MSG()
    window.user.GetMessageW.argtypes=(ctypes.POINTER(w.MSG),w.HWND,w.UINT,w.UINT)
    window.user.TranslateMessage.argtypes=(ctypes.POINTER(w.MSG),)
    window.user.DispatchMessageW.argtypes=(ctypes.POINTER(w.MSG),);window.user.DispatchMessageW.restype=ctypes.c_ssize_t
    window.user.IsDialogMessageW.argtypes=(w.HWND,ctypes.POINTER(w.MSG));window.user.IsDialogMessageW.restype=w.BOOL
    while window.user.GetMessageW(ctypes.byref(msg),None,0,0)>0:
        if not window.user.IsDialogMessageW(window.hwnd,ctypes.byref(msg)):
            window.user.TranslateMessage(ctypes.byref(msg));window.user.DispatchMessageW(ctypes.byref(msg))
    worker.join(16)
    if worker.is_alive():os._exit(0)
    window.gdi.DeleteObject.argtypes=(w.HANDLE,)
    for resource in window.paint_resources:window.gdi.DeleteObject(resource)
    for icon in (window.icon,window.small_icon):
        if icon:window.user.DestroyIcon(icon)
    window.user.UnregisterClassW.argtypes=(w.LPCWSTR,w.HINSTANCE)
    window.user.UnregisterClassW(window.wc.name,window.instance)
    _window=None
    return window.exit_code
