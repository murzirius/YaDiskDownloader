import threading
import time
from types import SimpleNamespace

import pytest

from conftest import stop_engines, test_timeout, wait_job
from disk_downloader.api import Suspended
from disk_downloader.engine import Control


def test_wait_job_waits_for_worker_without_repeated_status_queries():
    calls = []
    worker = threading.Thread(target=lambda: time.sleep(0.15))
    engine = SimpleNamespace(threads={'job': worker}, status=lambda job: calls.append(job) or {'phase': 'ready', 'live': False})
    worker.start()
    assert wait_job(engine, 'job')['phase'] == 'ready'
    assert calls == ['job']


def test_cleanup_stops_paused_worker_and_releases_report_lock():
    from disk_downloader.reports import REPORT_LOCK
    control = Control()
    control.pause()
    entered = threading.Event()

    def blocked():
        with REPORT_LOCK:
            entered.set()
            try:
                control.check()
            except Suspended:
                pass

    worker = threading.Thread(target=blocked, daemon=True)
    engine = SimpleNamespace(controls={'job': control}, threads={'job': worker}, shutting_down=False)
    worker.start()
    try:
        assert entered.wait(2)
    finally:
        stop_engines([engine])
    assert engine.shutting_down and control.stopped and not worker.is_alive()
    assert REPORT_LOCK.acquire(timeout=1)
    REPORT_LOCK.release()


def test_scheduling_timeout_factor_is_bounded_and_explicit(monkeypatch):
    monkeypatch.setenv('YADISK_TEST_TIMEOUT_FACTOR', '4')
    assert test_timeout(3) == 12
    with monkeypatch.context() as invalid:
        invalid.setenv('YADISK_TEST_TIMEOUT_FACTOR', 'nan')
        with pytest.raises(ValueError):
            test_timeout(3)
