import sqlite3
import threading
from performance_fixtures import seed_summary

def test_unarmed_scheduler_does_not_access_sqlite(system,monkeypatch):
    from disk_downloader.management import Management
    engine,api,tmp=system
    def unexpected():raise AssertionError('idle queue opened SQLite')
    monkeypatch.setattr(engine.store,'db',unexpected)
    manager=Management(engine)
    for _ in range(20):manager.tick()


def test_notification_observer_detects_external_commits_without_pinning_reader(system):
    from disk_downloader.tray import Tray
    import time
    engine,api,tmp=system
    key=seed_summary(engine.store,tmp,'fallback','new',0,empty=True)
    delivered=threading.Event()
    class Icon:
        visible=True
        def notify(self,text,title):delivered.set()
    tray=Tray(engine,'',lambda:None);tray.icon=Icon()
    worker=threading.Thread(target=tray.watch_notifications);worker.start()
    try:
        due=time.monotonic()+5
        while key not in tray.seen and time.monotonic()<due:time.sleep(.01)
        assert key in tray.seen
        with sqlite3.connect(engine.store.filename,timeout=2) as external:
            external.execute("UPDATE jobs SET phase='completed' WHERE id=?",(key,))
        assert delivered.wait(5)
    finally:
        tray.notification_stop.set();worker.join(5)
    assert not worker.is_alive()
