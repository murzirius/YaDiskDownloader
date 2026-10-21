"""Release the optional OS sleep inhibitor on pause and task completion."""
import os
import shutil
import subprocess
import sys
import threading
from .runtime import external_environment


class SleepGuard:
    def __init__(self, control, enabled):
        self.control,self.enabled=control,enabled
        self.finished=threading.Event();self.thread=None;self.process=None

    def __enter__(self):
        if self.enabled:
            self.thread=threading.Thread(target=self.watch,name='disk-sleep-inhibitor',daemon=True)
            try:self.thread.start()
            except RuntimeError:
                self.thread=None
                self.control.event('system','Не удалось включить запрет сна. Проверьте настройки питания устройства.','', 'warning')
        return self

    def __exit__(self,*_):
        self.finished.set()
        if self.thread:self.thread.join(3)

    def watch(self):
        active=False
        try:
            while not self.finished.is_set():
                needed=not self.control.paused and not self.control.stopped
                if needed and self.process and self.process.poll() is not None:
                    raise OSError('Sleep inhibitor was rejected')
                if needed!=active:
                    self.set(needed);active=needed
                self.finished.wait(.5)
        except (OSError,ValueError):
            self.control.event('system','Система не разрешила запрет сна. Проверьте настройки питания устройства. Загрузка продолжается.','', 'warning')
        finally:
            try:self.set(False)
            except OSError:pass

    def set(self, enabled):
        if os.name=='nt':
            import ctypes
            if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000):raise OSError('Sleep inhibitor unavailable')
            return
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
            self.process=None
        if not enabled:return
        if sys.platform=='darwin':command=['/usr/bin/caffeinate','-i','-w',str(os.getpid())]
        elif shutil.which('systemd-inhibit') and shutil.which('tail'):
            command=['systemd-inhibit','--what=sleep','--mode=block','--who=YaDiskDownloader','--why=File download',shutil.which('tail'),'--pid='+str(os.getpid()),'-f','/dev/null']
        else:raise OSError('Sleep inhibitor unavailable')
        self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=external_environment())
