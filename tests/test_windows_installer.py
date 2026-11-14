"""Build, install, upgrade and remove the real Inno Setup installer with synthetic payload."""
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from disk_downloader import __version__
from disk_downloader.windows_install import required_files

if sys.platform != 'win32':
    pytest.skip('Windows installer', allow_module_level=True)

from tools import build_installer

APP_ID_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B7C1F7A4-3E52-4C8E-9A0B-6D5F0E2C41A9}_is1'


def installed_on_this_pc():
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_ID_KEY))
        return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def remove_test_installation():
    """A failed test must never leave an installation registered for this user."""
    yield
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_ID_KEY) as key:
            command = winreg.QueryValueEx(key, 'UninstallString')[0].split('"')[1]
    except OSError:
        return
    if 'pytest' in command:
        subprocess.run([command, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/StateDir=' + str(Path(command).parent / 'none' / 'YaDiskDownloader')], timeout=120)


def payload_zip(path, exe=b'MZ synthetic executable'):
    with zipfile.ZipFile(path, 'w') as archive:
        for name in required_files():
            archive.writestr('YaDiskDownloader/' + name, exe if name.endswith('.exe') else b'Synthetic ' + name.encode())
        archive.writestr('YaDiskDownloader/docs/usage.md', b'usage')
    return path


def run(command):
    # A freshly written EXE can be briefly locked by a virus scanner.
    for attempt in range(20):
        try:
            return subprocess.run(command, check=True, timeout=180)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.5)


def wait_gone(path):
    for _ in range(300):
        if not path.exists():
            return
        time.sleep(.1)
    raise AssertionError(f'{path} still exists')


@pytest.fixture(scope='module')
def setup_exe(tmp_path_factory):
    try:
        build_installer.find_iscc()
    except SystemExit:
        pytest.skip('Inno Setup is not installed')
    if installed_on_this_pc():
        pytest.skip('A real YaDiskDownloader installation exists; the test would replace it')
    folder = tmp_path_factory.mktemp('installer')
    archive = payload_zip(folder / 'one.zip')
    return build_installer.build(archive, folder / 'out'), folder


def install_args(setup, target, log):
    return [str(setup), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/NOCANCEL',
            f'/DIR={target}', f'/LOG={log}']


def test_installer_name_matches_release_zip_prefix():
    assert build_installer.installer_name() == f'YaDiskDownloader-v{__version__}-Windows-x64-Setup.exe'
    # The updater only accepts the exact ZIP asset name; the installer must never look like it.
    assert not build_installer.installer_name().endswith('.zip')


def test_incomplete_zip_is_rejected(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('YaDiskDownloader/YaDiskDownloader.exe', b'x')
    with pytest.raises(ValueError):
        build_installer.build(archive, tmp_path / 'out')


def test_zip_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / 'evil.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('YaDiskDownloader/../evil.txt', b'x')
    with pytest.raises(ValueError):
        build_installer.unpack(archive, tmp_path / 'stage')


def test_install_upgrade_and_uninstall_keep_history(setup_exe, tmp_path):
    setup, folder = setup_exe
    target, state = tmp_path / 'Programs' / 'YaDiskDownloader', tmp_path / 'YaDiskDownloader'
    state.mkdir()
    (state / 'jobs.sqlite3').write_bytes(b'private history')
    run(install_args(setup, target, tmp_path / 'install.log'))
    if True:
        for name in required_files():
            assert (target / name).is_file()
        assert (target / 'unins000.exe').is_file()
        assert (target / 'docs/usage.md').is_file()

        # The in-app updater replaces the EXE in place and may leave temporary files.
        (target / 'YaDiskDownloader.exe').write_bytes(b'MZ updated by application')
        (target / '.update-0123.exe').write_bytes(b'stale')
        (target / 'install-files.json').write_text('{}')
        newer = payload_zip(folder / 'two.zip', exe=b'MZ newer synthetic executable')
        newer_setup = build_installer.build(newer, folder / 'out2')
        run(install_args(newer_setup, target, tmp_path / 'upgrade.log'))
        assert (target / 'YaDiskDownloader.exe').read_bytes() == b'MZ newer synthetic executable'
        assert (state / 'jobs.sqlite3').read_bytes() == b'private history'
    uninstall = [str(target / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/StateDir={state}']
    run(uninstall)
    wait_gone(target)
    assert (state / 'jobs.sqlite3').read_bytes() == b'private history'
    assert not installed_on_this_pc()


def test_uninstall_can_remove_history_only_when_asked(setup_exe, tmp_path):
    setup, _ = setup_exe
    target, state = tmp_path / 'App', tmp_path / 'YaDiskDownloader'
    state.mkdir()
    (state / 'jobs.sqlite3').write_bytes(b'private history')
    run(install_args(setup, target, tmp_path / 'install.log'))
    run([str(target / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
         f'/StateDir={state}', '/DeleteHistory=yes'])
    wait_gone(target)
    wait_gone(state)
    assert not installed_on_this_pc()
