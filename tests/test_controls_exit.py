import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from conftest import make_job, test_timeout, wait_job
from disk_downloader.engine import Control
from disk_downloader.store import Store
from disk_downloader.tray import Tray


def test_old_jobs_and_profiles_are_reduced_to_eight(system):
    engine, _, tmp = system
    job = make_job(system)
    settings = engine.store.job(job)['settings'] | {'workers': 20, 'adaptive_workers': False}
    with engine.store.db() as db:
        db.execute('UPDATE jobs SET settings=? WHERE id=?', (json.dumps(settings), job))
        db.execute('INSERT INTO profiles VALUES(?,?)', ('Old', json.dumps(settings)))
    restored = Store(engine.store.filename)
    assert restored.job(job)['settings']['workers'] == 8
    profile = next(restored.rows('SELECT settings FROM profiles'))
    assert json.loads(profile['settings'])['workers'] == 8
    with pytest.raises(ValueError):
        engine.validate_settings(settings | {'workers': 9})


def test_scan_can_stop_and_continue_without_starting_download(system, monkeypatch):
    engine, api, tmp = system
    for number in range(205):
        api.file(f'disk:/{number:04}.bin', preview=False)
    reached = threading.Event()
    blocking = [True]
    original = api.metadata

    def metadata(*args, **kwargs):
        if kwargs.get('offset') == 200 and blocking[0]:
            reached.set()
            while blocking[0]:
                args[2]()
                time.sleep(.01)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, 'metadata', metadata)
    job = engine.create({'destination': str(tmp/'downloads'), 'whole_disk': True})
    assert reached.wait(test_timeout(10))
    engine.action(job, 'stop')
    assert wait_job(engine, job)['phase'] == 'stopped'
    blocking[0] = False
    engine.action(job, 'continue_scan')
    ready = wait_job(engine, job)
    assert ready['phase'] == 'ready' and ready['files'] == 205
    assert api.media_calls == []


def test_stop_aborts_active_requests_immediately():
    control = Control()
    aborted = threading.Event()
    class ActiveRequest:
        network_aborted = threading.Event()
        abort = staticmethod(aborted.set)
    deadline = ActiveRequest()
    control.network.register(deadline)
    control.stop()
    assert control.stopped and aborted.is_set()


def test_tray_menu_does_not_wait_for_engine_lock(system):
    engine, _, _ = system
    control = Control()
    engine.controls['test'] = control
    engine.threads['test'] = SimpleNamespace(is_alive=lambda: True)
    tray = Tray(engine, '', lambda: None)
    locked, release, returned = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with engine.lock:
            locked.set()
            release.wait()

    holder = threading.Thread(target=hold)
    reader = threading.Thread(target=lambda: (tray.active(), returned.set()))
    holder.start()
    try:
        assert locked.wait(2)
        reader.start()
        assert returned.wait(1)
    finally:
        release.set()
        holder.join(2)
        reader.join(2)
        del engine.threads['test']
        del engine.controls['test']


@pytest.mark.parametrize('mode', ['idle', 'scan', 'hung'])
def test_real_process_exits_and_releases_port_and_state(tmp_path, mode):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('shutdown_worker.py')), str(tmp_path), mode, str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    session = requests.Session()
    session.trust_env = False
    base = f'http://127.0.0.1:{port}'
    try:
        due = time.monotonic() + test_timeout(20)
        while time.monotonic() < due:
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1].decode(errors='replace'))
            try:
                page = session.get(base+'/', timeout=.5)
                if page.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(.05)
        else:
            raise AssertionError('Test server did not start')
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        endpoint = '/api/test-tray-exit' if mode == 'scan' else '/api/exit'
        response = session.post(base+endpoint, json={}, headers={'X-CSRF-Token': csrf, 'Origin': base}, timeout=5)
        assert response.status_code == 200
        process.wait(timeout=10)
        assert process.returncode == 0
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0
        from run import lock_state
        lock = lock_state(tmp_path/'state')
        lock.close()
        store = Store(tmp_path/'state/jobs.sqlite3')
        if mode == 'scan':
            job = (tmp_path/'job').read_text()
            assert store.job(job)['phase'] in ('stopped', 'interrupted')
    finally:
        session.close()
        if process.poll() is None:
            process.kill()
            process.wait(5)
