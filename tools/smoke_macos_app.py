"""Mount a real DMG, install to a temporary folder and run the Cocoa application."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from disk_downloader.cli import Client
import requests


def run(command, timeout=60):
    return subprocess.run([str(part) for part in command], check=True, capture_output=True, text=True, timeout=timeout)


def wait_client(port, state, process):
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        try:
            return Client(port, state)
        except requests.ConnectionError:
            if process.poll() is not None:
                raise AssertionError('The macOS application closed before its server became ready')
            time.sleep(0.2)
    raise AssertionError('The macOS application did not start within 65 seconds')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dmg', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run this check on macOS')
    dmg = args.dmg.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='yadisk-macos-installer-') as folder:
        root = Path(folder)
        mount = root / 'mounted'
        mount.mkdir()
        run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', mount, dmg])
        try:
            source = mount / 'YaDiskDownloader.app'
            assert (mount / 'Applications').is_symlink()
            assert (mount / 'Applications').readlink() == Path('/Applications')
            assert (mount / 'Как установить.txt').is_file()
            app = root / 'Installed Applications' / source.name
            app.parent.mkdir()
            run(['ditto', source, app])
            assert digest(source / 'Contents/MacOS/yadisk') == digest(app / 'Contents/MacOS/yadisk')
        finally:
            run(['hdiutil', 'detach', mount])
        run(['codesign', '--verify', '--deep', '--strict', app])
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        assert info['NSHighResolutionCapable']
        assert (app / 'Contents/Resources/AppIcon.icns').stat().st_size > 10000
        state = root / 'state'
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        launcher = subprocess.Popen(['open', '-n', '-W', str(app), '--args', '--port', str(port), '--state-dir', str(state), '--no-browser'])
        client = None
        try:
            client = wait_client(port, state, launcher)
            runtime = client.call('/api/runtime')
            assert runtime['frozen']
            resource = client.session.get(client.base + '/static/file-icons.svg', timeout=5)
            assert resource.ok and b'<symbol id="folder"' in resource.content
            assert client.call('/api/status')['jobs'] == []
            # The installed executable remains usable as a terminal client.
            cli = run([app / 'Contents/MacOS/yadisk', '--port', str(port), '--state-dir', state, '--json', 'status'])
            assert json.loads(cli.stdout)['jobs'] == []
            # Exercise Cocoa's actual Quit action, not an HTTP replacement for it.
            script = 'on run argv\ntell application (item 1 of argv) to quit\nend run'
            run(['osascript', '-e', script, str(app)], timeout=95)
            launcher.wait(timeout=45)
            assert launcher.returncode == 0
            with socket.socket() as probe:
                assert probe.connect_ex(('127.0.0.1', port)) != 0, 'Quit left the local server running'
            result = {'actual_app': True, 'actual_dmg': True, 'version': runtime['version'],
                      'readonly_mount': True, 'drag_install_equivalent': True, 'after_image_eject': True,
                      'signature_verified': True, 'hi_dpi_icon': True, 'finder_launch': True,
                      'browser_resources': True, 'packaged_cli': True, 'cocoa_quit': True,
                      'server_fully_stopped': True, 'apple_notarized': False}
            args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result))
        finally:
            if client:
                try:
                    client.call('/api/exit', {})
                except (requests.RequestException, ValueError):
                    pass
            if launcher.poll() is None:
                # Best effort UI cleanup; no process matching outside this test app.
                try:
                    run(['osascript', '-e', 'on run argv\ntell application (item 1 of argv) to quit\nend run', str(app)], timeout=45)
                    launcher.wait(timeout=10)
                except (subprocess.SubprocessError, OSError):
                    launcher.terminate()
                    launcher.wait(timeout=5)


if __name__ == '__main__':
    main()
