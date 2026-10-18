"""Native Windows control window. All filesystem/network work stays off its UI thread."""
import os
import queue
import threading
import time

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


def launch(main):
    import ctypes
    from ctypes import wintypes as w
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
            self.command_busy=False;self.snapshot_busy=False;self.job=None;self.jobs=[];self.tick=0
            self.instance=self.kernel.GetModuleHandleW(None)
            self.gdi=ctypes.WinDLL('gdi32')
            self.gdi.CreateFontW.argtypes=(ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.LPCWSTR)
            self.gdi.CreateFontW.restype=w.HANDLE
            self.font=self.gdi.CreateFontW(-16,0,0,0,400,0,0,0,1,0,0,5,0,'Segoe UI')
            self.heading_font=self.gdi.CreateFontW(-29,0,0,0,600,0,0,0,1,0,0,5,0,'Segoe UI')
            self.label_font=self.gdi.CreateFontW(-13,0,0,0,600,0,0,0,1,0,0,5,0,'Segoe UI')
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
            self.hwnd=self.user.CreateWindowExW(0,self.wc.name,'YaDiskDownloader — управление',0x02CA0000,160,100,800,705,None,None,self.instance,None)
            if not self.hwnd:raise ctypes.WinError(ctypes.get_last_error())
            try:
                dark=w.BOOL(True);dwm=ctypes.WinDLL('dwmapi')
                dwm.DwmSetWindowAttribute.argtypes=(w.HWND,w.DWORD,ctypes.c_void_p,w.DWORD)
                dwm.DwmSetWindowAttribute(self.hwnd,20,ctypes.byref(dark),ctypes.sizeof(dark))
            except OSError:pass
            ctypes.WinDLL('comctl32').InitCommonControls()
            self.controls={}
            self.control('STATIC','YaDiskDownloader',30,23,700,40,1)
            self.user.SendMessageW(self.controls[1],0x0030,self.heading_font,1)
            self.control('STATIC','ПАНЕЛЬ УПРАВЛЕНИЯ',32,69,700,20,20)
            self.control('STATIC','СОСТОЯНИЕ ПРИЛОЖЕНИЯ',48,116,680,20,21)
            self.control('STATIC','Подготавливаем приложение…',48,144,680,45,2)
            self.control('msctls_progress32','',48,202,680,8,3)
            self.control('STATIC','СОХРАНЁННОЕ ЗАДАНИЕ',48,248,680,20,22)
            self.control('COMBOBOX','',48,278,680,210,4,0x00200003)
            self.control('STATIC','Задания появятся после запуска приложения.',48,322,680,44,5)
            self.control('BUTTON','Продолжить',48,379,176,40,11)
            self.control('BUTTON','Пауза',234,379,124,40,12)
            self.control('BUTTON','Остановить',368,379,166,40,13)
            self.control('BUTTON','Открыть папку',544,379,184,40,14)
            self.control('STATIC','При дозагрузке',48,439,170,25,6)
            self.control('COMBOBOX','',226,435,502,160,7,0x00200003)
            self.add_options(7,['Нужны оригиналы','Превью достаточно','Оригиналы и превью']);self.user.SendMessageW(self.controls[7],0x014E,0,0)
            self.control('STATIC','Подключение Яндекс.Диска',48,494,480,22,10)
            self.control('EDIT','',48,524,488,30,8,0x008000A0)
            self.control('BUTTON','Подключить ключ',548,522,180,36,15)
            self.control('STATIC','Ключ хранится в памяти. Новые задания создаются в веб-интерфейсе.',48,567,680,24,9)
            self.control('BUTTON','Открыть веб-интерфейс',30,618,230,38,16)
            self.control('BUTTON','Обновить',272,618,132,38,17)
            self.control('BUTTON','Свернуть',416,618,142,38,18)
            self.control('BUTTON','Выйти',570,618,184,38,19)
            for key in (20,21,22,9):self.user.SendMessageW(self.controls[key],0x0030,self.label_font,1)
            for key in (11,12,13,14,15,16,17):self.user.EnableWindow(self.controls[key],False)
            self.user.SetTimer(self.hwnd,1,150,None)
            self.user.ShowWindow(self.hwnd,5)

        def control(self,kind,text,x,y,width,height,key,extra=0):
            handle=self.user.CreateWindowExW(0,kind,text,0x50000000|extra|(0x0000000b if kind=='BUTTON' else 0)|(0x00010000 if kind in ('BUTTON','COMBOBOX','EDIT') else 0),x,y,width,height,self.hwnd,key,self.instance,None)
            self.controls[key]=handle
            self.user.SendMessageW(handle,0x0030,self.font,1)
            if kind in ('COMBOBOX','EDIT','msctls_progress32'):
                theme=ctypes.WinDLL('uxtheme')
                theme.SetWindowTheme.argtypes=(w.HWND,w.LPCWSTR,w.LPCWSTR)
                theme.SetWindowTheme(handle,'','')
            if kind=='msctls_progress32':
                self.user.SendMessageW(handle,0x2001,0,0x342b25)
                self.user.SendMessageW(handle,0x0409,0,0xe1c3a8)

        def add_options(self,key,values):
            self.user.SendMessageW(self.controls[key],0x014B,0,0)
            for text in values:
                buffer=ctypes.create_unicode_buffer(text)
                self.user.SendMessageW(self.controls[key],0x0143,0,ctypes.cast(buffer,ctypes.c_void_p).value)

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
            disabled=bool(item.state&4);primary=item.id in (11,16)
            brush=self.accent_brush if primary and not disabled else self.focus_brush if item.state&1 else self.button_brush
            self.user.FillRect(item.dc,ctypes.byref(item.rect),self.panel if item.id in (11,12,13,14,15) else self.background)
            old=self.gdi.SelectObject(item.dc,brush)
            pen=self.gdi.SelectObject(item.dc,self.gdi.GetStockObject(8))
            r=item.rect;self.gdi.RoundRect(item.dc,r.left,r.top,r.right,r.bottom,12,12)
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
            for top,bottom in ((102,226),(234,480),(488,601)):self.gdi.RoundRect(dc,30,top,754,bottom,18,18)
            self.gdi.SelectObject(dc,pen);self.gdi.SelectObject(dc,old);self.user.EndPaint(hwnd,ctypes.byref(paint))

        def close(self):
            if self.closing:return
            self.closing=True
            if self.shutdown:self.shutdown()
            self.user.DestroyWindow(self.hwnd)

        def command(self,action):
            if self.command_busy or not self.app or self.closing:return
            self.command_busy=True;job_id=self.job['id'] if self.job else None
            policy=('originals','any_result','both')[max(0,self.user.SendMessageW(self.controls[7],0x0147,0,0))]
            token=None
            if action=='connect':
                buffer=ctypes.create_unicode_buffer(4097);self.user.GetWindowTextW(self.controls[8],buffer,len(buffer));token=buffer.value;self.text(8,'')
            def work():
                try:
                    if action=='connect':
                        import re,requests
                        with requests.Session() as session:
                            session.trust_env=False
                            page=session.get(self.url+'/',timeout=10);page.raise_for_status()
                            csrf=re.search(r'name="csrf-token" content="([^"]+)"',page.text).group(1)
                            result=session.post(self.url+'/api/connect',json={'token':token},headers={'Origin':self.url,'X-CSRF-Token':csrf},timeout=25)
                            if not result.ok:raise ValueError(result.json().get('error','Яндекс.Диск не подтвердил ключ. Повторите подключение.'))
                        text='Диск подключён. Выберите задание и нажмите «Начать / продолжить».'
                    elif action=='folder':
                        from .paths import safe_join
                        if not job_id:raise ValueError('Сначала выберите задание.')
                        root=safe_join(self.app.extensions['store'].job(job_id)['root'],'')
                        os.startfile(root,'explore');text='Папка результатов открыта.'
                    else:
                        if not job_id:raise ValueError('Сначала создайте задание в полном интерфейсе.')
                        engine=self.app.extensions['engine'];job=engine.store.job(job_id)
                        verb=('start' if job['phase']=='ready' else 'resume') if action=='resume' else action
                        limits={'missing_policy':policy} if verb in ('start','resume') else None
                        engine.action(job_id,verb,limits);text='Команда выполнена. Состояние обновляется.'
                    self.messages.put(('notice',text))
                except Exception as exc:
                    from .engine import safe_error
                    self.messages.put(('notice',str(exc) if isinstance(exc,ValueError) else safe_error(exc)))
                finally:self.messages.put(('command_done',None))
            try:threading.Thread(target=work,daemon=True,name='disk-desktop-action').start()
            except RuntimeError:
                self.command_busy=False
                self.text(5,'Приложение не смогло начать команду. Повторите действие.')

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
                        jobs=list(engine.store.rows('SELECT id,title,phase FROM jobs ORDER BY started DESC LIMIT 100'))
                    else:jobs=self.jobs
                    job_id=active or chosen or (jobs[0]['id'] if jobs else None)
                    snapshot=engine.status_cache.snapshot(engine.store.job(job_id)) if job_id else None
                    self.messages.put(('snapshot',(jobs,snapshot)))
                except Exception:
                    self.messages.put(('notice','Приложение на устройстве не смогло получить состояние задания. Нажмите «Обновить задания».'))
                finally:self.messages.put(('snapshot_done',None))
            try:threading.Thread(target=work,daemon=True,name='disk-desktop-status').start()
            except RuntimeError:
                self.snapshot_busy=False
                self.text(5,'Не удалось начать обновление состояния. Нажмите «Обновить задания».')

        def drain(self):
            while True:
                try:kind,value=self.messages.get_nowait()
                except queue.Empty:break
                if kind=='progress':self.text(2,value[0]);self.user.SendMessageW(self.controls[3],0x0402,value[1],0)
                elif kind=='notice':self.text(5,value)
                elif kind=='command_done':self.command_busy=False;self.refresh(True)
                elif kind=='snapshot_done':self.snapshot_busy=False
                elif kind=='ready':
                    self.text(2,'Приложение готово. Управляйте загрузкой здесь или откройте полный интерфейс.');self.user.SendMessageW(self.controls[3],0x0402,100,0)
                    for key in (15,16,17):self.user.EnableWindow(self.controls[key],True)
                    self.refresh(True)
                elif kind=='snapshot':
                    jobs,job=value;self.jobs=jobs;self.job=job
                    self.add_options(4,[item['title'] or item['id'][:12] for item in jobs])
                    if job:
                        selected=next((i for i,item in enumerate(jobs) if item['id']==job['id']),0);self.user.SendMessageW(self.controls[4],0x014E,selected,0)
                        job['_settled']=not job.get('live') and not job.get('statistics_pending')
                        from .journal import PHASES
                        action=job.get('active',[{}]);detail=action[0].get('action','') if action else ''
                        self.text(2,(PHASES.get(job['phase'],job['phase']))+(' · '+detail if detail else ''))
                        counts='Уточняем число файлов. Управление заданием доступно.' if job.get('statistics_pending') else f"Файлов обработано: {job.get('processed',0)} / {job.get('files',0)}. Оригиналы: {job.get('originals',0)}. Превью: {job.get('previews',0)}."
                        current=action[0] if action else {}
                        percent=int(job.get('percent') or 0)
                        if current.get('unit') in ('records','objects') and current.get('size') is not None:
                            counts=f"{'Записей истории' if current['unit']=='records' else 'Объектов'}: {current['done']} / {current['size']}"
                            percent=int(min(100,current['done']*100/max(1,current['size'])))
                        self.text(5,job.get('pause_reason') or job.get('error') or job.get('network_warning') or counts)
                        self.user.SendMessageW(self.controls[3],0x0402,percent,0)
                        self.user.EnableWindow(self.controls[12],bool(job.get('can_pause')))
                        self.user.EnableWindow(self.controls[13],bool(job.get('live')))
                        resumable=job['phase'] in ('ready','paused','stopped','interrupted','failed')
                        self.user.EnableWindow(self.controls[11],resumable and (self.app.extensions['engine'].api.credentials.connected or job.get('requires_connection') is False))
                        self.user.EnableWindow(self.controls[14],True)
                        self.user.EnableWindow(self.controls[7],not job.get('live'))
                        if getattr(self,'policy_job',None)!=job['id']:
                            self.policy_job=job['id']
                            self.user.SendMessageW(self.controls[7],0x014E,('originals','any_result','both').index(job['settings'].get('missing_policy','originals')),0)
                    else:
                        self.text(5,'Сохранённых заданий нет. Откройте полный интерфейс, чтобы создать загрузку.')
                elif kind=='finished':
                    self.closing=True;self.user.DestroyWindow(self.hwnd)
                elif kind=='failed':
                    self.text(2,'Не удалось запустить приложение.');self.text(5,value)
            self.tick+=1
            if self.tick%14==0 and not self.user.IsIconic(self.hwnd):self.refresh()

        def wndproc(self,hwnd,message,wp,lp):
            try:
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
                    if key in (9,20,21,22):self.gdi.SetTextColor(wp,self.muted_text)
                    return self.background if key in (1,20) else self.panel
                if message==0x8001:self.user.ShowWindow(hwnd,9);self.user.SetForegroundWindow(hwnd);return 0
                if message==0x0113:self.drain();return 0
                if message==0x0111:
                    key=wp&0xffff
                    if key==4 and wp>>16==1:
                        index=self.user.SendMessageW(self.controls[4],0x0147,0,0)
                        if 0<=index<len(self.jobs):self.job={'id':self.jobs[index]['id']};self.refresh(True)
                    elif key in (11,12,13,14,15):self.command({11:'resume',12:'pause',13:'stop',14:'folder',15:'connect'}[key])
                    elif key==16:
                        import webbrowser
                        if self.url:webbrowser.open(self.url)
                    elif key==17:self.refresh(True)
                    elif key==18:self.user.ShowWindow(hwnd,6)
                    elif key==19:self.close()
                    return 0
            except Exception:
                if hasattr(self,'controls'):self.text(5,'Приложение на устройстве не выполнило команду. Повторите или откройте полный интерфейс.')
            return self.user.DefWindowProcW(hwnd,message,wp,lp)
    window=Window();_window=window
    # The native window is already visible before importing Flask/SQLite or migrating.
    def startup():
        try:main();window.messages.put(('finished',None))
        except BaseException:
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
    window.user.UnregisterClassW.argtypes=(w.LPCWSTR,w.HINSTANCE)
    window.user.UnregisterClassW(window.wc.name,window.instance)
    _window=None
    return 0
