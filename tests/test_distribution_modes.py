import json
import sys
from pathlib import Path
import subprocess

import pytest
import requests


def test_gui_entry_does_not_dispatch_terminal_commands(tmp_path):
    entry = Path(__file__).resolve().parents[1] / 'run.py'
    result = subprocess.run([sys.executable, '-X', 'utf8', str(entry), 'status'], cwd=tmp_path,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 2
    assert 'unrecognized arguments: status' in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_public_cli_rejects_non_linux_before_connecting(platform, monkeypatch, capsys):
    from disk_downloader import cli
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(cli, 'Client', lambda *_: pytest.fail('Unsupported CLI reached the local service'))
    assert cli.main(['status']) == 2
    assert 'только в Linux' in capsys.readouterr().err


def test_macos_private_service_cannot_create_downloads(monkeypatch):
    from disk_downloader import macos_service
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(macos_service, 'Client', lambda *_: pytest.fail('Invalid command reached the service'))
    with pytest.raises(SystemExit) as result:
        macos_service.main(['download'])
    assert result.value.code == 2


def test_macos_private_service_starts_and_checks_same_state(monkeypatch, tmp_path, capsys):
    from disk_downloader import macos_service
    monkeypatch.setattr(sys, 'platform', 'darwin')
    calls = []
    def start(port, state, *, stop_on_failure=False):
        calls.append((port, state, stop_on_failure))
        return {'running': True}
    monkeypatch.setattr(macos_service, 'start_background', start)
    assert macos_service.main(['--port', '12345', '--state-dir', str(tmp_path), 'start']) == 0
    assert calls == [(12345, tmp_path, True)]
    assert json.loads(capsys.readouterr().out)['running']


def test_macos_private_stop_is_safe_when_server_is_already_closed(monkeypatch, capsys):
    from disk_downloader import macos_service
    monkeypatch.setattr(sys, 'platform', 'darwin')
    def unavailable(*_):
        raise requests.ConnectionError('offline')
    monkeypatch.setattr(macos_service, 'Client', unavailable)
    assert macos_service.main(['stop']) == 0
    assert json.loads(capsys.readouterr().out) == {'ok': True}
