"""Private lifecycle worker for the macOS application; not a download CLI."""
import argparse
import json
from pathlib import Path
import sys

import requests

from .service_control import Client, start_background


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='backslashreplace')
            except (OSError, ValueError):
                pass
    if sys.platform != 'darwin':
        print('Этот служебный процесс используется только приложением macOS.', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description='Internal macOS application service')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('command', choices=('start', 'status', 'stop'))
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('Порт должен быть от 1024 до 65535')
    try:
        if args.command == 'start':
            result = start_background(args.port, args.state_dir, stop_on_failure=True)
        else:
            try:
                client = Client(args.port, args.state_dir)
                result = client.call('/api/exit', {}) if args.command == 'stop' else client.call('/api/status')
            except requests.ConnectionError:
                if args.command != 'stop':
                    raise
                result = {'ok': True}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (requests.RequestException, ValueError, OSError) as error:
        print('Приложение на этом Mac: ' + str(error), file=sys.stderr)
        return 2
