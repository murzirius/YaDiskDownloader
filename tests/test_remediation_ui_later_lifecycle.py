"""Synthetic owned-child lifecycle regressions without native process execution."""
import subprocess

import pytest
import requests

from disk_downloader import service_control


class OwnedProcess:
    def __init__(self, exited=False, escalate=False):
        self.pid = 54321
        self.returncode = 2 if exited else None
        self.exited = exited
        self.escalate = escalate
        self.actions = []

    def poll(self):
        return self.returncode if self.exited else None

    def terminate(self):
        self.actions.append('terminate')

    def wait(self, timeout=None):
        self.actions.append(('wait', timeout))
        if self.escalate and 'kill' not in self.actions:
            raise subprocess.TimeoutExpired('synthetic-owned-child', timeout)
        self.exited = True
        self.returncode = -9 if 'kill' in self.actions else -15
        return self.returncode

    def kill(self):
        self.actions.append('kill')


def setup_child(monkeypatch, failure, *, exited=False, escalate=False):
    child = OwnedProcess(exited=exited, escalate=escalate)
    calls = []

    def client(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise requests.ConnectionError('not running')
        raise failure

    monkeypatch.setattr(service_control, 'Client', client)
    monkeypatch.setattr(service_control.subprocess, 'Popen', lambda *args, **kwargs: child)
    return child, calls


@pytest.mark.parametrize('failure', [ValueError('synthetic invalid readiness response'), requests.ReadTimeout('synthetic timeout'), OSError('synthetic read failure')])
def test_macos_start_failure_cleans_up_only_its_owned_child(tmp_path, monkeypatch, failure):
    child, calls = setup_child(monkeypatch, failure)
    with pytest.raises(type(failure)):
        service_control.start_background(8766, tmp_path, stop_on_failure=True)
    assert child.actions[0] == 'terminate'
    assert child.actions[1][0] == 'wait'
    assert len(child.actions) == 2
    assert len(calls) == 2


def test_linux_default_keeps_detached_child_on_readiness_failure(tmp_path, monkeypatch):
    child, _ = setup_child(monkeypatch, ValueError('synthetic readiness failure'))
    with pytest.raises(ValueError):
        service_control.start_background(8766, tmp_path)
    assert child.actions == []


def test_macos_start_timeout_reaps_owned_child(tmp_path, monkeypatch):
    child, _ = setup_child(monkeypatch, ValueError('unused'))
    ticks = iter((0, 46))
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(service_control.time, 'monotonic', lambda: next(ticks))
        with pytest.raises(ValueError):
            service_control.start_background(8766, tmp_path, stop_on_failure=True)
    assert child.actions[0] == 'terminate'
    assert child.exited


def test_macos_owned_child_cleanup_escalates_bounded_wait(tmp_path, monkeypatch):
    child, _ = setup_child(monkeypatch, ValueError('synthetic readiness failure'), escalate=True)
    with pytest.raises(ValueError):
        service_control.start_background(8766, tmp_path, stop_on_failure=True)
    assert [action for action in child.actions if isinstance(action, str)] == ['terminate', 'kill']
    waits = [action[1] for action in child.actions if isinstance(action, tuple)]
    assert len(waits) == 2 and all(0 < timeout <= 5 for timeout in waits)
    assert child.exited


def test_macos_does_not_signal_already_exited_owned_child(tmp_path, monkeypatch):
    child, _ = setup_child(monkeypatch, ValueError('synthetic readiness failure'), exited=True)
    with pytest.raises(ValueError):
        service_control.start_background(8766, tmp_path, stop_on_failure=True)
    assert child.actions == []


def test_existing_ready_service_is_never_terminated(tmp_path, monkeypatch):
    monkeypatch.setattr(service_control, 'Client', lambda *args, **kwargs: object())
    monkeypatch.setattr(service_control.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('must not spawn'))
    assert service_control.start_background(8766, tmp_path, stop_on_failure=True)['running'] is True


def test_macos_private_launcher_chooses_owned_failure_cleanup(monkeypatch, capsys):
    from disk_downloader import macos_service
    seen = []
    monkeypatch.setattr(macos_service.sys, 'platform', 'darwin')
    monkeypatch.setattr(macos_service, 'start_background', lambda port, state, **kwargs: seen.append(kwargs) or {'running': True})
    assert macos_service.main(['start']) == 0
    assert seen == [{'stop_on_failure': True}]


def test_actual_macos_helper_readiness_failure_does_not_leave_owned_service(tmp_path, monkeypatch, capsys):
    from disk_downloader import macos_service
    child, _ = setup_child(monkeypatch, ValueError('synthetic startup failure'))
    monkeypatch.setattr(macos_service.sys, 'platform', 'darwin')
    assert macos_service.main(['--state-dir', str(tmp_path), 'start']) == 2
    assert child.actions[0] == 'terminate'
    assert child.exited


def test_successfully_started_owned_service_remains_running(tmp_path, monkeypatch):
    child = OwnedProcess()
    calls = []
    def client(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise requests.ConnectionError('not running')
        return object()
    monkeypatch.setattr(service_control, 'Client', client)
    monkeypatch.setattr(service_control.subprocess, 'Popen', lambda *args, **kwargs: child)
    result = service_control.start_background(8766, tmp_path, stop_on_failure=True)
    assert result['running'] and result['pid'] == child.pid
    assert child.actions == [] and not child.exited


def test_foreign_service_error_never_starts_or_signals_a_child(tmp_path, monkeypatch):
    def client(*args, **kwargs):
        raise ValueError('another application owns this port')
    monkeypatch.setattr(service_control, 'Client', client)
    monkeypatch.setattr(service_control.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('must not spawn'))
    with pytest.raises(ValueError, match='another application'):
        service_control.start_background(8766, tmp_path, stop_on_failure=True)
