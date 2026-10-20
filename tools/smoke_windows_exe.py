"""Exercise the real frozen binary, including a native tray close message."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

import requests


def wait_until(check, seconds, description):
    due = time.monotonic()+seconds
    while time.monotonic() < due:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError(description)


def children(pid):
    # Query only children of our bootloader; no unrelated application details.
    command = f'@(Get-CimInstance Win32_Process -Filter "ParentProcessId={pid}" | Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress'
    value = subprocess.check_output(['powershell','-NoProfile','-Command',command], text=True).strip()
    ids = json.loads(value) if value else []
    return [ids] if isinstance(ids, int) else ids


def native_tray_window(pids):
    import win32gui
    import win32process
    matches = []
    def visit(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids and win32gui.GetClassName(hwnd).startswith('YaDiskDownloader') and win32gui.GetClassName(hwnd) != 'YaDiskDownloaderDesktop':
            matches.append(hwnd)
    win32gui.EnumWindows(visit, None)
    # pystray 0.19.5 creates icon window first and menu helper second. EnumWindows
    # lists them in reverse Z order; only the icon is registered in its dispatcher.
    return matches[-1] if len(matches) == 2 else None


def native_status_window(pids):
    import ctypes
    import win32gui
    import win32process
    matches = []
    def visit(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] in pids and win32gui.GetClassName(hwnd) == 'YaDiskDownloaderDesktop':
            matches.append(hwnd)
    win32gui.EnumWindows(visit, None)
    if len(matches) != 1:return None
    hwnd = matches[0]
    controls = []
    win32gui.EnumChildWindows(hwnd, lambda child, _: controls.append((win32gui.GetClassName(child), win32gui.GetWindowText(child))), None)
    assert not any(kind in ('Edit', 'ComboBox') for kind, _ in controls), controls
    assert {text for kind, text in controls if kind == 'Button'} == {'Открыть в браузере', 'Свернуть', 'Выход'}, controls
    if not any('Прервано' in text for _, text in controls):return None
    user = ctypes.WinDLL('user32')
    user.GetWindowDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    user.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    user.AreDpiAwarenessContextsEqual.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    assert user.AreDpiAwarenessContextsEqual(user.GetWindowDpiAwarenessContext(hwnd), ctypes.c_void_p(-4)), 'Mini window is bitmap-scaled instead of PerMonitorV2'
    return hwnd


def seed_interrupted_history(directory):
    from disk_downloader.store import Store
    from disk_downloader.engine import Engine
    from disk_downloader.api import DiskAPI, Credentials
    root = Path(directory)/'Результаты'
    root.mkdir()
    (root/'Вложенная папка').mkdir()
    store = Store(Path(directory)/'state/jobs.sqlite3')
    settings = Engine(store, DiskAPI(Credentials())).validate_settings({
        'destination':str(root),'whole_disk':True,'auto_pause':False})
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete) VALUES(?,?,?,?,?,?,?)',
                   ('interrupted-smoke',json.dumps(settings),str(root),str(root),'downloading','2026-01-01T00:00:00Z',1))
        db.execute("INSERT INTO job_queue(job,position,action,state) VALUES('interrupted-smoke',1,'resume','running')")
    return root


def run(executable, output):
    if os.name != 'nt':
        raise RuntimeError('Windows only')
    executable = executable.resolve(strict=True)
    results = []
    for exit_mode in ('web', 'native_tray', 'native_window'):
        with tempfile.TemporaryDirectory(prefix='yadisk-exe-smoke-') as directory:
            root = seed_interrupted_history(directory)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0))
                port = probe.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = {key:value for key,value in os.environ.items() if not key.startswith('PYTHON')}
            env['PATH'] = str(Path(os.environ['SystemRoot'])/'System32')
            command = [str(executable),'--no-browser','--port',str(port),'--state-dir',str(Path(directory)/'state')]
            if exit_mode == 'native_window':command.append('--window')
            process = subprocess.Popen(command, cwd=directory, env=env)
            session = requests.Session()
            session.trust_env = False
            owned_pids = []
            started = time.monotonic()
            try:
                def page():
                    assert process.poll() is None, 'EXE exited during startup'
                    try:
                        response = session.get(base+'/',timeout=1)
                        return response if response.status_code == 200 else None
                    except requests.RequestException:
                        return None
                response = wait_until(page,60,'Frozen EXE did not start')
                token = re.search(r'name="csrf-token" content="([^"]+)"',response.text).group(1)
                headers = {'X-CSRF-Token':token, 'Origin':base}
                def runtime():
                    result = session.get(base+'/api/runtime',headers=headers,timeout=3)
                    result.raise_for_status()
                    value = result.json()
                    return value if value['tray']['visible'] else None
                info = wait_until(runtime,20,'Native tray icon did not start')
                assert info['frozen'] and info['safe_sqlite_wal'], info
                from disk_downloader import __build__
                assert info['build'] == __build__, info
                state = session.get(base+'/api/status',headers=headers,timeout=3).json()
                assert state['active_job'] is None and state['jobs'][0]['phase'] == 'interrupted', state
                queue = session.get(base+'/api/queue',headers=headers,timeout=3).json()
                assert queue['paused'] and queue['needs_confirmation'] and queue['items'][0]['state'] == 'waiting', queue
                folders = session.get(base+'/api/local-folders',params={'path':str(root)},headers=headers,timeout=3)
                folders.raise_for_status()
                assert folders.json()['folders'] == [{'name':'Вложенная папка','path':str(root/'Вложенная папка')}], folders.text
                status = session.get(base+'/api/jobs/interrupted-smoke',headers=headers,timeout=3)
                status.raise_for_status()
                assert not status.json()['live'] and status.json()['can_resume'], status.text
                for asset in re.findall(r'(?:src|href)="(/static/[^"?]+)',response.text):
                    asset_response = session.get(base+asset,timeout=5)
                    assert asset_response.status_code == 200, asset
                    expected = Path(__file__).resolve().parents[1]/'disk_downloader'/asset.lstrip('/')
                    assert asset_response.content == expected.read_bytes(), 'Stale frozen asset: '+asset
                assert session.post(base+'/api/exit',json={},headers={'Origin':base},timeout=3).status_code == 403
                owned_pids = children(process.pid)
                assert owned_pids, 'Frozen child process was not found'
                hwnd = wait_until(lambda:native_tray_window(owned_pids),10,'Native tray window is missing')
                quit_started = time.monotonic()
                if exit_mode == 'web':
                    quit_response = session.post(base+'/api/exit',json={},headers=headers,timeout=5)
                    quit_response.raise_for_status()
                elif exit_mode == 'native_tray':
                    import win32gui
                    win32gui.PostMessage(hwnd,0x0010,0,0)  # WM_CLOSE -> Tray.exit
                else:
                    import win32gui
                    window = wait_until(lambda:native_status_window(owned_pids),10,'Mini status window is missing')
                    win32gui.PostMessage(window,0x0010,0,0)
                process.wait(timeout=20)
                assert process.returncode == 0
                with socket.socket() as probe:
                    assert probe.connect_ex(('127.0.0.1',port)) != 0, 'Port remained open'
                import win32api
                import win32event
                for pid in owned_pids:
                    try:
                        handle = win32api.OpenProcess(0x100000,False,pid)
                    except win32api.error as exc:
                        assert exc.winerror == 87, exc
                    else:
                        try:
                            assert win32event.WaitForSingleObject(handle,1000) == 0, 'Child process remained alive'
                        finally:
                            handle.Close()
                from run import lock_state
                lock_state(Path(directory)/'state').close()
                results.append({'exit':exit_mode,'runtime':info,'startup_seconds':round(quit_started-started,2),'exit_seconds':round(time.monotonic()-quit_started,2),'port_released':True,'children_exited':True,'local_folder_picker':True,'interrupted_history':True,'queue_waits_for_user':True})
            finally:
                session.close()
                if process.poll() is None:
                    # Only the process tree created by this test.
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                    process.wait(timeout=10)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=True,indent=2))


if __name__ == '__main__':
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe',required=True,type=Path)
    parser.add_argument('--output',type=Path,default=Path('smoke-results.json'))
    args = parser.parse_args()
    run(args.exe,args.output)
