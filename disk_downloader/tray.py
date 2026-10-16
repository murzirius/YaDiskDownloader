import threading
import webbrowser
import os
from PIL import Image, ImageDraw


def image():
    icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((3, 3, 61, 61), radius=14, fill="#1c1f24", outline="#a8c3e1", width=3)
    draw.line((32, 14, 32, 39), fill="#a8c3e1", width=6)
    draw.line((21, 30, 32, 41, 43, 30), fill="#a8c3e1", width=5)
    draw.line((18, 49, 46, 49), fill="#e2e5ea", width=4)
    return icon


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
                        webbrowser.open(owner.url)
                    else:
                        super()._on_notify(wparam,lparam)
            icon_class=ClickableIcon
        self.icon = icon_class("YaDiskDownloader", image(), "YaDiskDownloader", menu=pystray.Menu(
            pystray.MenuItem("Открыть приложение", lambda icon, item: webbrowser.open(self.url), default=True),
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
        first = True
        while not self.notification_stop.wait(1):
            try:
                with self.engine.store.db() as db:
                    pref = db.execute("SELECT value FROM preferences WHERE key='notifications'").fetchone()
                    rows = db.execute('SELECT id,phase,generation,pause_reason FROM jobs').fetchall()
                enabled = not pref or pref[0] == 'true'
                for job in rows:
                    key = (job['phase'], job['generation'], bool(job['pause_reason']))
                    previous = self.seen.get(job['id'])
                    self.seen[job['id']] = key
                    if first or previous == key or not enabled or not self.icon or not self.icon.visible:
                        continue
                    text = None
                    if job['phase'] == 'completed': text = 'Загрузка завершена. Откройте приложение через значок в трее.'
                    elif job['phase'] == 'completed_with_errors': text = 'Загрузка завершена с замечаниями. Проверьте результат в приложении.'
                    elif job['phase'] == 'paused' and job['pause_reason']: text = 'Загрузка на паузе: проверьте свободное место на диске.'
                    elif job['phase'] == 'failed': text = 'Задание требует внимания. Откройте приложение через значок в трее.'
                    if text: self.icon.notify(text, 'YaDiskDownloader')
                first = False
            except Exception:
                continue

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
