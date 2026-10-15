import argparse
import os
import socket
import sys
import threading
import webbrowser
import json
import re
from pathlib import Path

from waitress import create_server

from disk_downloader.web import create_app
from disk_downloader.runtime import frozen, state_folder, program_folder
from disk_downloader.migration import migrate, candidates
from disk_downloader.tray import Tray
from disk_downloader.lifecycle import Shutdown


def message(text):
    if sys.stdout:
        print(text, flush=True)
    elif os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "YaDiskDownloader", 0x40)


def lock_state(folder):
    folder.mkdir(parents=True, exist_ok=True)
    stream = (folder / "server.lock").open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException:
        stream.close()
        raise


def main():
    parser = argparse.ArgumentParser(description="Локальное скачивание Яндекс.Диска")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--state-dir")
    parser.add_argument('--apply-update')
    parser.add_argument('--update-ack')
    args = parser.parse_args()
    if args.apply_update:
        if not frozen() or os.name != 'nt':
            parser.error('Режим обновления доступен в Windows EXE')
        from disk_downloader.updater import apply_update
        apply_update(args.apply_update)
        return 0
    if not 1024 <= args.port <= 65535:
        parser.error("Порт должен быть от 1024 до 65535")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
    except OSError:
        listener.close()
        message(f"Порт {args.port} занят. Если приложение уже запущено, откройте его через значок в трее. Для отдельного запуска выберите --port.")
        return 1
    state = Path(args.state_dir) if args.state_dir else state_folder()
    try:
        state_lock = lock_state(state)
    except OSError:
        listener.close()
        message("Папка состояния занята другим процессом или недоступна. Завершите его или выберите отдельную --state-dir.")
        return 1
    migration = migrate(state, candidates(program_folder())) if not args.state_dir else {"jobs": 0, "failed_sources": 0}
    app = create_app(state, port=args.port)
    app.extensions["migration"] = migration
    server = create_server(app, sockets=[listener], threads=12, clear_untrusted_proxy_headers=True)
    url = f"http://127.0.0.1:{args.port}"
    if sys.stdout:
        print(f"Приложение доступно: {url}", flush=True)
    shutdown = Shutdown(app, server, state_lock)
    app.extensions["quit_app"] = shutdown
    app.extensions['management'].start()
    if os.name == "nt" and not args.no_tray:
        try:
            tray = Tray(app.extensions["engine"], url, shutdown)
            app.extensions["tray"] = tray
            tray.start()
        except Exception:
            message("Не удалось создать значок в трее. Завершение доступно в меню «Обновления и диагностика».")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if args.update_ack:
        from disk_downloader import __version__
        ack = Path(args.update_ack).resolve()
        if ack.name != 'ready.json' or not re.fullmatch('[a-f0-9]{32}', ack.parent.name) or ack.parent.parent != (state.resolve() / 'updates'):
            raise ValueError('Некорректный путь подтверждения обновления')
        with ack.open('x', encoding='utf-8') as stream:
            json.dump({'version':__version__}, stream)
            stream.flush(); os.fsync(stream.fileno())
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    except OSError:
        if not shutdown.requested.is_set():
            raise
    finally:
        shutdown()
        shutdown.done.wait(shutdown.force_after + 1)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        if frozen():
            message("Не удалось запустить YaDiskDownloader. Проверьте доступ к папке состояния и свободное место на диске.")
            sys.exit(1)
        raise
