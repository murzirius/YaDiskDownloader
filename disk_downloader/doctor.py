"""Read-only application checks outside HTTP workers."""
import shutil
import threading
import time
from pathlib import Path

from .sqlite_support import safe_wal


class Doctor:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.thread = None
        self.result = {'busy':False, 'checks':[], 'checked_at':None}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():return dict(self.result)
            self.result = {'busy':True, 'checks':[], 'checked_at':None}
            self.thread = threading.Thread(target=self.run, name='disk-doctor', daemon=True)
            self.thread.start()
        return self.snapshot()

    def snapshot(self):
        with self.lock:return dict(self.result, checks=list(self.result['checks']))

    def run(self):
        checks = []
        try:
            with self.engine.store.db() as db:
                okay = db.execute('PRAGMA quick_check(1)').fetchone()[0] == 'ok'
            checks.append({'source':'Приложение', 'ok':okay, 'message':'История прошла быструю проверку' if okay else 'История требует восстановления. Сохраните диагностику; не удаляйте базу.'})
            usage = shutil.disk_usage(Path(self.engine.store.filename).parent)
            checks.append({'source':'Локальное хранилище', 'ok':usage.free>64*1024*1024, 'message':f'Свободно для истории: {usage.free//1024//1024} МиБ'})
            checks.append({'source':'SQLite', 'ok':True, 'message':'Используется безопасный WAL' if safe_wal() else 'Используется совместимый журнал DELETE'})
            connected = self.engine.api.credentials.connected
            checks.append({'source':'Яндекс.Диск', 'ok':connected, 'message':'Ключ подключён; его действительность проверяется запросом к Диску' if connected else 'Ключ не подключён. Введите его перед загрузкой.'})
        except Exception:
            checks.append({'source':'Приложение', 'ok':False, 'message':'Не удалось завершить проверку. Сохраните диагностику.'})
        finally:
            with self.lock:self.result = {'busy':False, 'checks':checks, 'checked_at':time.time()}
