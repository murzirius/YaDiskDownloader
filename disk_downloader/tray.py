import threading
import webbrowser
import os
from PIL import Image, ImageDraw


def image(size=64):
    # Draw at four times the target resolution for crisp small and high-DPI icons.
    scale=size*4/64
    icon = Image.new('RGBA',(size*4,size*4),(0,0,0,0))
    draw = ImageDraw.Draw(icon)
    points=lambda values:tuple(round(value*scale) for value in values)
    draw.rounded_rectangle(points((3,3,61,61)),radius=round(14*scale),fill='#1c1f24',outline='#a8c3e1',width=round(3*scale))
    for values,width in (((32,14,32,39),6),((21,30,32,41,43,30),5),((18,49,46,49),4)):
        draw.line(points(values),fill='#e2e5ea' if width==4 else '#a8c3e1',width=round(width*scale))
    return icon.resize((size,size),Image.Resampling.LANCZOS)


class Tray:
    def __init__(self, engine, url, quit_app):
        self.engine = engine
        self.url = url
        self.quit_app = quit_app
        self.icon = None
        self.error = ""
        self.thread = None
        self.ready = threading.Event()
        self.notification_stop = threading.Event()
        self.notification_thread = None
        self.seen = {}
        self.exiting = threading.Event()

    def exit(self):
        if not self.exiting.is_set():
            self.exiting.set()
            self.notification_stop.set()
            self.quit_app()

    def open(self):
        from .desktop import show
        if not show():webbrowser.open(self.url)

    def active(self):
        for job_id, thread in tuple(self.engine.threads.items()):
            control = self.engine.controls.get(job_id)
            if control and thread.is_alive():
                return job_id, control
        return None

    def toggle_async(self):
        threading.Thread(target=self.toggle, name='disk-tray-action', daemon=True).start()

    def toggle(self):
        active = self.active()
        if active:
            try:
                self.engine.action(active[0], "resume" if active[1].paused else "pause")
            except ValueError as exc:
                self.error = str(exc)
                if self.icon:
                    self.icon.notify(self.error, "YaDiskDownloader")

    def pause_label(self):
        active = self.active()
        return "Продолжить" if active and active[1].paused else "Пауза"

    def start(self):
        import pystray
        icon_class=pystray.Icon
        if os.name=='nt':
            owner=self
            class ClickableIcon(pystray.Icon):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self._message_handlers[0x0010] = lambda w, l: owner.exit()  # WM_CLOSE

                def _on_notify(self,wparam,lparam):
                    if lparam==0x405:  # NIN_BALLOONUSERCLICK (WM_USER + 5)
                        owner.open()
                    else:
                        super()._on_notify(wparam,lparam)
            icon_class=ClickableIcon
        self.icon = icon_class("YaDiskDownloader", image(), "YaDiskDownloader", menu=pystray.Menu(
            pystray.MenuItem("Открыть приложение", lambda icon, item: self.open(), default=True),
            pystray.MenuItem(lambda item: self.pause_label(), lambda icon, item: self.toggle_async(), enabled=lambda item: bool(self.active())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Завершить приложение", lambda icon, item: self.exit(), enabled=lambda item: not self.exiting.is_set())
        ))
        def setup(icon):
            icon.visible = True
            self.ready.set()
        def run():
            try:
                self.icon.run(setup)
            except Exception:
                self.error = "Не удалось показать значок в трее. Завершение доступно в веб-интерфейсе."
                self.ready.set()
        self.thread = threading.Thread(target=run, name="disk-tray", daemon=True)
        self.thread.start()
        self.notification_thread = threading.Thread(target=self.watch_notifications, name='disk-notifications', daemon=True)
        self.notification_thread.start()

    def watch_notifications(self):
        from .work_policy import quiet

        def allowed(settings):
            try:
                return not (quiet(settings) and settings.get('quiet_hours',{}).get('mute',True))
            except (ValueError,TypeError,AttributeError):
                # A damaged task must not break notifications for other tasks.
                return False

        first = True
        while not self.notification_stop.is_set():
            try:
                # A read-only, autocommit observer keeps data_version meaningful
                # across ticks. No read transaction is held between polls.
                with self.engine.store.db() as db:
                    previous_version = None
                    enabled = True
                    warned_health = None
                    settings_by_job = {}
                    while not self.notification_stop.wait(1):
                        observe_health = previous_version is not None
                        active = self.active()
                        version = db.execute('PRAGMA data_version').fetchone()[0]
                        if version != previous_version:
                            pref = db.execute("SELECT CAST(value AS BLOB) FROM preferences WHERE key='notifications'").fetchone()
                            rows = db.execute('SELECT id,phase,generation,pause_reason,CAST(settings AS BLOB) AS settings FROM jobs').fetchall()
                            previous_version = version
                            enabled = not pref
                            if pref:
                                try:
                                    value = pref[0].decode(self.engine.store.text_encoding) if isinstance(pref[0],bytes) else pref[0]
                                except UnicodeError:
                                    value = None
                                enabled = value == 'true'
                            current = {}
                            settings_by_job = {}
                            for job in rows:
                                key = (job['phase'], job['generation'], bool(job['pause_reason']))
                                previous = self.seen.get(job['id'])
                                current[job['id']] = key
                                health_job = enabled and active and active[0] == job['id']
                                phase_notice = not first and previous != key and enabled and self.icon and self.icon.visible
                                if not health_job and not phase_notice:
                                    continue
                                try:
                                    settings = self.engine.store.settings_value(job['settings'])
                                except (ValueError,TypeError):
                                    continue
                                if health_job:
                                    # Keep only the active task's small policy, not
                                    # every saved task's full settings and paths.
                                    settings_by_job[job['id']] = {'quiet_hours':settings.get('quiet_hours',{})}
                                if not phase_notice or not allowed(settings):
                                    continue
                                text = None
                                if job['phase'] == 'completed': text = 'Загрузка завершена. Откройте приложение через значок в трее.'
                                elif job['phase'] == 'completed_with_errors': text = 'Загрузка завершена с замечаниями. Проверьте результат в приложении.'
                                elif job['phase'] == 'paused' and job['pause_reason']: text = job['pause_reason']
                                elif job['phase'] == 'failed': text = 'Задание требует внимания. Откройте приложение через значок в трее.'
                                if text:
                                    self.icon.notify(text, 'YaDiskDownloader')
                            self.seen = current
                            first = False
                        if observe_health:
                            warning = active[1].health()['warning'] if active else ''
                            key = (active[0],warning) if active and warning else None
                            permitted = enabled and active and active[0] in settings_by_job and allowed(settings_by_job[active[0]])
                            if key and key != warned_health and permitted and self.icon and self.icon.visible:
                                self.icon.notify(warning,'YaDiskDownloader')
                                warned_health = key
                            elif not key or not permitted:
                                warned_health = None
            except Exception:
                self.notification_stop.wait(1)

    def stop(self):
        self.notification_stop.set()
        if self.notification_thread:
            self.notification_thread.join(2)
        if self.icon:
            self.icon.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(3)

    def status(self):
        return {"visible": bool(self.icon and self.ready.is_set() and self.icon.visible), "error": self.error}
