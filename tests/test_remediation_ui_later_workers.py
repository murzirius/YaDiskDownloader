"""Synthetic notification and Doctor regressions; no native UI or account used."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from disk_downloader.doctor import Doctor
from disk_downloader.store import Store
from disk_downloader.tray import Tray


class Ticks:
    def __init__(self, count=3, on_tick=lambda number: None):
        self.tick = 0
        self.count = count
        self.on_tick = on_tick

    def is_set(self):
        return self.tick >= self.count

    def wait(self, seconds):
        self.tick += 1
        self.on_tick(self.tick)
        return self.is_set()


class Icon:
    visible = True

    def __init__(self):
        self.messages = []

    def notify(self, message, title):
        self.messages.append((message, title))


def notification_fixture(tmp_path, settings, *, enabled=True, warning='synthetic stalled storage', encoding=None):
    filename = tmp_path / 'synthetic.sqlite3'
    if encoding is not None:
        with sqlite3.connect(filename) as db:
            db.execute("PRAGMA encoding='" + encoding + "'")
            db.execute('CREATE TABLE encoding_seed(value TEXT)')
    store = Store(filename)
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)', ('good', json.dumps(settings), str(tmp_path), str(tmp_path), 'downloading', 'synthetic'))
        db.execute("INSERT INTO preferences(key,value) VALUES('notifications',?)", ('true' if enabled else 'false',))
    control = SimpleNamespace(health=lambda: {'warning': warning}, policy=SimpleNamespace(settings=settings))
    engine = SimpleNamespace(store=store, threads={'good': SimpleNamespace(is_alive=lambda: True)}, controls={'good': control})
    tray = Tray(engine, 'http://127.0.0.1:1', lambda: None)
    tray.icon = Icon()
    return tray, store


@pytest.mark.parametrize('quiet_enabled,mute,globally_enabled,expected', [(True,True,True,0),(True,False,True,1),(False,True,True,1),(False,False,False,0)])
def test_health_notifications_respect_quiet_mute_and_global_preference(tmp_path, monkeypatch, quiet_enabled, mute, globally_enabled, expected):
    from disk_downloader import work_policy
    monkeypatch.setattr(work_policy, 'quiet', lambda value: value.get('quiet_hours',{}).get('enabled',False))
    tray, _ = notification_fixture(tmp_path, {'quiet_hours': {'enabled': quiet_enabled, 'mute': mute}}, enabled=globally_enabled)
    tray.notification_stop = Ticks()
    tray.watch_notifications()
    assert len(tray.icon.messages) == expected


@pytest.mark.parametrize('invalid', [b'\xff', b'{broken', b'[]'])
def test_bad_saved_settings_do_not_suppress_other_jobs_notifications(tmp_path, monkeypatch, invalid):
    from disk_downloader import work_policy
    monkeypatch.setattr(work_policy, 'quiet', lambda value: False)
    tray, store = notification_fixture(tmp_path, {}, warning='')
    with store.db() as db:
        db.execute("DELETE FROM jobs WHERE id='good'")
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,CAST(? AS TEXT),?,?,?,?)', ('bad', invalid, str(tmp_path), str(tmp_path), 'failed', 'synthetic'))
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)', ('good', '{}', str(tmp_path), str(tmp_path), 'downloading', 'synthetic'))

    def tick(number):
        if number == 2:
            with store.db() as db:
                db.execute("UPDATE jobs SET phase='completed' WHERE id IN ('good','bad')")
    tray.notification_stop = Ticks(count=4, on_tick=tick)
    tray.watch_notifications()
    assert len(tray.icon.messages) == 1
    assert 'Загрузка завершена' in tray.icon.messages[0][0]


def test_doctor_thread_start_failure_resets_busy_and_allows_retry(monkeypatch):
    from disk_downloader import doctor as doctor_module
    real_thread = threading.Thread
    doctor = Doctor(SimpleNamespace())

    class BrokenThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError('synthetic OS thread exhaustion')
        def is_alive(self):
            return False

    monkeypatch.setattr(doctor_module.threading, 'Thread', BrokenThread)
    result = doctor.start()
    assert result['busy'] is False
    assert result['checks'] and result['checks'][0]['ok'] is False
    assert doctor.thread is None

    def successful_check():
        with doctor.lock:
            doctor.result = {'busy': False, 'checks': [{'source': 'synthetic', 'ok': True, 'message': 'retry works'}], 'checked_at': 1}
    monkeypatch.setattr(doctor, 'run', successful_check)
    monkeypatch.setattr(doctor_module.threading, 'Thread', real_thread)
    doctor.start()
    doctor.thread.join(2)
    assert not doctor.thread.is_alive()
    assert doctor.snapshot()['checks'][0]['message'] == 'retry works'


def test_health_policy_updates_at_quiet_boundary_without_database_changes(tmp_path, monkeypatch):
    from disk_downloader import work_policy
    state = {'quiet': True}
    monkeypatch.setattr(work_policy, 'quiet', lambda value: state['quiet'])
    tray, _ = notification_fixture(tmp_path, {'quiet_hours': {'enabled': True, 'mute': True}})
    def tick(number):
        if number == 3:
            state['quiet'] = False
    tray.notification_stop = Ticks(count=4, on_tick=tick)
    tray.watch_notifications()
    assert len(tray.icon.messages) == 1


def test_health_observer_does_not_decode_inactive_history_settings(tmp_path, monkeypatch):
    from disk_downloader import work_policy
    monkeypatch.setattr(work_policy, 'quiet', lambda value: False)
    tray, store = notification_fixture(tmp_path, {})
    with store.db() as db:
        db.executemany('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                       ((f'old-{index}', json.dumps({'paths': ['synthetic'] * 100}), str(tmp_path), str(tmp_path), 'completed', 'synthetic') for index in range(200)))
    parsed = []
    original = store.settings_value
    def parse(value):
        parsed.append(value)
        return original(value)
    monkeypatch.setattr(store, 'settings_value', parse)
    tray.notification_stop = Ticks()
    tray.watch_notifications()
    assert len(tray.icon.messages) == 1
    assert len(parsed) == 1



@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
def test_notification_preference_respects_database_text_encoding(tmp_path, monkeypatch, encoding):
    from disk_downloader import work_policy
    monkeypatch.setattr(work_policy, 'quiet', lambda value: False)
    tray, store = notification_fixture(tmp_path, {}, encoding=encoding)
    assert store.text_encoding == encoding.lower().replace('utf-16le', 'utf-16-le').replace('utf-16be', 'utf-16-be')
    tray.notification_stop = Ticks()
    tray.watch_notifications()
    assert len(tray.icon.messages) == 1


def test_invalid_notification_preference_fails_closed_without_observer_retry(tmp_path, monkeypatch):
    from disk_downloader import work_policy
    monkeypatch.setattr(work_policy, 'quiet', lambda value: False)
    tray, store = notification_fixture(tmp_path, {})
    with store.db() as db:
        db.execute("UPDATE preferences SET value=CAST(? AS TEXT) WHERE key='notifications'", (b'\xff',))
    tray.notification_stop = Ticks()
    tray.watch_notifications()
    assert tray.icon.messages == []
    assert tray.seen.get('good') == ('downloading', 0, False)
