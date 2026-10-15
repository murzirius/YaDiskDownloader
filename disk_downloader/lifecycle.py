"""One bounded exit path shared by the tray, HTTP API and console."""
import logging
import os
import threading
import time


class Shutdown:
    def __init__(self, app, server, state_lock, *, grace=10, force_after=15, force_exit=os._exit):
        self.app, self.server, self.state_lock = app, server, state_lock
        self.grace, self.force_after, self.force_exit = grace, force_after, force_exit
        self.requested = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def __call__(self):
        with self.lock:
            if self.requested.is_set():
                return
            self.requested.set()
            self.app.extensions['engine'].shutting_down = True
            # If Python remains in executor atexit hooks, terminate only this
            # process. Durable file/SQLite checkpoints are recovered next launch.
            timer = threading.Timer(self.force_after, self.force_exit, args=(0,))
            timer.daemon = True
            timer.start()
            self.thread = threading.Thread(target=self.finish, name='disk-shutdown', daemon=True)
            self.thread.start()

    @staticmethod
    def attempt(operation):
        try:
            operation()
        except Exception:
            logging.getLogger(__name__).exception('Shutdown step failed')

    def finish(self):
        deadline = time.monotonic() + self.grace
        engine = self.app.extensions['engine']
        manager, maintenance = self.app.extensions['management'], self.app.extensions['maintenance']
        manager.stopped.set()
        maintenance.cancel.set()
        for control in tuple(engine.controls.values()):
            self.attempt(control.stop)
        tray = self.app.extensions.get('tray')
        if tray:
            self.attempt(tray.stop)
        # Abort sockets before closing response bodies, which can otherwise
        # wait on a blocked reader in another thread.
        self.attempt(engine.api.credentials.clear)
        self.attempt(self.server.close)
        channels = getattr(self.server, '_map', getattr(self.server, 'map', {}))
        for channel in list(channels.values()):
            self.attempt(channel.close)
        self.attempt(lambda: self.server.task_dispatcher.shutdown(timeout=1))
        workers = list(engine.threads.values())
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            return  # Watchdog finishes a non-cooperative executor.
        self.attempt(self.state_lock.close)
        self.done.set()
