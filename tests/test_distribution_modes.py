import json
import sys

import pytest
import requests


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
    def start(port, state):
        calls.append((port, state))
        return {'running': True}
    monkeypatch.setattr(macos_service, 'start_background', start)
    assert macos_service.main(['--port', '12345', '--state-dir', str(tmp_path), 'start']) == 0
    assert calls == [(12345, tmp_path)]
    assert json.loads(capsys.readouterr().out)['running']


def test_macos_private_stop_is_safe_when_server_is_already_closed(monkeypatch, capsys):
    from disk_downloader import macos_service
    monkeypatch.setattr(sys, 'platform', 'darwin')
    def unavailable(*_):
        raise requests.ConnectionError('offline')
    monkeypatch.setattr(macos_service, 'Client', unavailable)
    assert macos_service.main(['stop']) == 0
    assert json.loads(capsys.readouterr().out) == {'ok': True}
