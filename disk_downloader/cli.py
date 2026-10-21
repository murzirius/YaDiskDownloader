"""Terminal client; downloads belong to a separate local application process."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import requests

from .runtime import frozen, state_folder
from .single_instance import state_key

COMMANDS = {'start','connect','disconnect','download','jobs','status','pause','resume','retry','scan','stop','doctor','open','plan','limits'}


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
            raise ValueError('По выбранному порту работает другой экземпляр. Проверьте --port и --state-dir.')

    def call(self, path, data=None):
        try:
            response = self.session.request('GET' if data is None else 'POST', self.base+path, json=data, timeout=(3,30), allow_redirects=False)
        except requests.Timeout:
            raise ValueError('Приложение на этом устройстве не ответило за 30 с. Команда могла выполниться: проверьте status перед повтором.') from None
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
            if process.poll() is not None:raise ValueError(f'Приложение на этом устройстве завершилось при запуске (код {process.returncode}). Выполните start --foreground, чтобы увидеть причину в терминале.')
            time.sleep(.2)
    raise ValueError('Запуск занимает больше 45 с. Проверьте статус позже; повторный процесс не требуется.')


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if stream is not None and hasattr(stream,'reconfigure'):
            try:stream.reconfigure(encoding='utf-8',errors='backslashreplace')
            except (OSError,ValueError):pass
    parser = argparse.ArgumentParser(prog='yadisk', description='Управление YaDiskDownloader. Закрытие терминала не останавливает фоновые задания.')
    from . import __version__
    parser.add_argument('--version',action='version',version='YaDiskDownloader '+__version__)
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--json',action='store_true',help='Ответ в JSON для скриптов')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in sorted(COMMANDS):
        command=commands.add_parser(name)
        command.add_argument('--port',type=int,default=argparse.SUPPRESS)
        command.add_argument('--state-dir',type=Path,default=argparse.SUPPRESS)
        command.add_argument('--json',action='store_true',default=argparse.SUPPRESS)
        if name=='start':command.add_argument('--foreground',action='store_true',help='Запуск в этом терминале с подробными сообщениями; закрытие терминала остановит приложение')
        if name in ('pause','resume','retry','scan','plan','limits'):command.add_argument('id')
        if name=='stop':command.add_argument('id',nargs='?',help='Без ID безопасно закрывает приложение')
        if name=='connect':command.add_argument('--stdin',action='store_true',help='Прочитать ключ из stdin; ключ не сохраняется на диск')
        if name=='status':
            command.add_argument('id',nargs='?');command.add_argument('--watch',action='store_true')
        if name=='download':
            command.add_argument('path',nargs='?',default='disk:/');command.add_argument('--to',type=Path,required=True)
            command.add_argument('--mode',choices=['fallback','previews','both'],default='fallback')
            command.add_argument('--missing',action='store_true');command.add_argument('--run',action='store_true',help='После подготовки начать скачивание')
        if name=='limits':
            command.add_argument('--workers',type=int);command.add_argument('--speed',type=float)
    args=parser.parse_args(argv)
    if not 1024<=args.port<=65535:parser.error('Порт должен быть от 1024 до 65535')
    try:
        if args.command=='start' and args.foreground:
            from run import main as server_main
            previous = sys.argv
            try:
                sys.argv=['run.py','--no-browser','--no-window','--no-tray','--port',str(args.port)]
                if args.state_dir:sys.argv+=['--state-dir',str(args.state_dir.resolve())]
                return server_main()
            finally:sys.argv=previous
        elif args.command=='start':result=start_background(args.port,args.state_dir)
        else:
            client=Client(args.port,args.state_dir)
            command=args.command
            if command=='connect':
                token=sys.stdin.readline(16385).strip() if args.stdin else getpass.getpass('Ключ Яндекс.Диска (ввод скрыт): ')
                if not token or len(token)>16384:raise ValueError('Введите ключ доступа Яндекс.Диска')
                try:result=client.call('/api/connect',{'token':token})
                finally:token=None
            elif command=='disconnect':result=client.call('/api/disconnect',{})
            elif command=='jobs':result=client.call('/api/status')
            elif command=='status':
                endpoint='/api/jobs/'+args.id if args.id else '/api/status'
                while True:
                    result=client.call(endpoint)
                    if not args.watch:break
                    print(json.dumps(result,ensure_ascii=False) if args.json else describe(result),flush=True)
                    time.sleep(2)
            elif command=='download':
                result=client.call('/api/jobs',{'paths':[args.path],'whole_disk':args.path=='disk:/','destination':str(args.to.resolve()),'mode':args.mode,'operation':'missing' if args.missing else 'new','create_zip':False})
                if args.run:
                    client.call('/api/queue',{'action':'add','job':result['id']})
            elif command=='plan':result=client.call('/api/jobs/'+args.id+'/plan')
            elif command=='limits':
                data={}
                if args.workers is not None:data['workers']=args.workers
                if args.speed is not None:data['speed_limit_mib']=args.speed
                result=client.call('/api/jobs/'+args.id+'/limits',data)
            elif command in ('pause','resume','retry','scan','stop'):
                result=client.call('/api/jobs/'+args.id+'/action',{'action':{'scan':'continue_scan'}.get(command,command)}) if args.id else client.call('/api/exit',{})
            elif command=='open':
                from .platform_actions import open_browser
                open_browser(client.base);result={'url':client.base}
            elif command=='doctor':result=client.call('/api/doctor')
        print(json.dumps(result,ensure_ascii=False) if args.json else describe(result))
        return 0
    except KeyboardInterrupt:
        print('\nПросмотр закрыт. Фоновые задания продолжают работу.');return 0
    except requests.ConnectionError:
        print('Приложение на этом устройстве недоступно. Выполните yadisk start.',file=sys.stderr);return 2
    except (requests.RequestException,ValueError,OSError) as error:
        print('Не удалось выполнить команду: '+str(error),file=sys.stderr);return 2


def describe(result):
    if 'jobs' in result:
        return '\n'.join(f"{job['id']}  {job['phase']}  {job.get('title') or 'Задание'}" for job in result['jobs']) or 'Заданий пока нет'
    if 'phase' in result:
        from .engine import PHASES
        return f"{result.get('id','')} · {PHASES.get(result['phase'],result['phase'])}\nФайлов: {result.get('files',0)} · готово: {result.get('processed',0)}\n"+(result.get('error') or '')
    return json.dumps(result,ensure_ascii=False,indent=2)
