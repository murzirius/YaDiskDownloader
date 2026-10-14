import threading
import webbrowser
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

    def active(self):
        with self.engine.lock:
            for job_id, thread in self.engine.threads.items():
                if thread.is_alive():
                    return job_id, self.engine.controls[job_id]
        return None

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
        self.icon = pystray.Icon("YaDiskDownloader", image(), "YaDiskDownloader", menu=pystray.Menu(
            pystray.MenuItem("Открыть приложение", lambda icon, item: webbrowser.open(self.url), default=True),
            pystray.MenuItem(lambda item: self.pause_label(), lambda icon, item: self.toggle(), enabled=lambda item: bool(self.active())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Завершить приложение", lambda icon, item: threading.Thread(target=self.quit_app, daemon=True).start())
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

    def stop(self):
        if self.icon:
            self.icon.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(3)

    def status(self):
        return {"visible": bool(self.icon and self.ready.is_set() and self.icon.visible), "error": self.error}
