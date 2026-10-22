"""Private local-service transport shared by the Linux CLI and macOS launcher."""
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import requests

from .runtime import frozen, state_folder
from .single_instance import state_key


class Client:
    def __init__(self, port=8766, state=None):
        self.base = f'http://127.0.0.1:{port}'
        self.session = requests.Session()
        self.session.trust_env = False
        try:
            response = self.session.get(self.base+'/', timeout=(2,10), allow_redirects=False)
        except requests.ReadTimeout:
            raise ValueError('Приложение на этом устройстве не ответило за 10 с. Проверьте его окно; повторный экземпляр не запускается.') from None
        response.raise_for_status()
        match = re.search(r'name="csrf-token" content="([A-Za-z0-9_-]{20,100})"', response.text)
        if not match:
            raise ValueError('По выбранному порту отвечает другая программа')
        self.session.headers.update({'Origin':self.base, 'X-CSRF-Token':match.group(1)})
        runtime = self.call('/api/runtime')
        if runtime.get('application') != 'YaDiskDownloader' or runtime.get('state_key') != state_key(state or state_folder()):
            raise ValueError('По выбранному порту работает другая копия приложения с иной историей. Завершите её или выберите другой порт запуска.')

    def call(self, path, data=None):
        try:
            response = self.session.request('GET' if data is None else 'POST', self.base+path, json=data, timeout=(3,30), allow_redirects=False)
        except requests.Timeout:
            raise ValueError('Приложение на этом устройстве не ответило за 30 с. Действие могло выполниться: проверьте состояние в интерфейсе перед повтором.') from None
        if response.is_redirect:
            raise ValueError('Приложение вернуло неожиданный адрес ответа')
        try:result = response.json()
        except ValueError:raise ValueError(f'Приложение на этом устройстве вернуло нечитаемый ответ HTTP {response.status_code}. Проверьте его окно и диагностику.') from None
        if not isinstance(result,dict):raise ValueError('Приложение вернуло неожиданный формат ответа')
        if not response.ok:
            raise ValueError(result.get('error', f'Ошибка приложения HTTP {response.status_code}'))
        return result


def start_background(port, state):
    try:
        Client(port, state)
        return {'running':True, 'url':f'http://127.0.0.1:{port}'}
    except requests.ConnectionError:
        pass
    state = Path(state or state_folder()).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [sys.executable] if frozen() else [sys.executable, str(Path(__file__).resolve().parents[1]/'run.py')]
    command += ['--serve','--no-browser','--no-window','--no-tray','--port',str(port),'--state-dir',str(state)]
    options = {'cwd':str(state), 'stdin':subprocess.DEVNULL, 'stdout':subprocess.DEVNULL, 'stderr':subprocess.DEVNULL, 'close_fds':True}
    if frozen():options['env'] = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    if os.name == 'nt':options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:options['start_new_session'] = True
    process = subprocess.Popen(command, **options)
    deadline = time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            Client(port,state)
            return {'running':True, 'pid':process.pid, 'url':f'http://127.0.0.1:{port}'}
        except requests.ConnectionError:
            if process.poll() is not None:raise ValueError(f'Приложение на этом устройстве завершилось при запуске (код {process.returncode}). Откройте приложение ещё раз и проверьте сообщение об ошибке.')
            time.sleep(.2)
    raise ValueError('Запуск занимает больше 45 с. Проверьте статус позже; повторный процесс не требуется.')


