import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from waitress import create_server

from disk_downloader.web import create_app


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
    parser.add_argument("--state-dir")
    args = parser.parse_args()
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
        print(f"Порт {args.port} занят. Откройте уже запущенное приложение или выберите --port.")
        return 1
    state = Path(args.state_dir) if args.state_dir else Path(__file__).resolve().parent / ".state"
    try:
        state_lock = lock_state(state)
    except OSError:
        listener.close()
        print("Папка состояния занята другим процессом или недоступна. Завершите его или выберите отдельную --state-dir.")
        return 1
    app = create_app(state, port=args.port)
    server = create_server(app, sockets=[listener], threads=12, clear_untrusted_proxy_headers=True)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Приложение доступно: {url}", flush=True)
    print("Токен вводится только в браузере. Ctrl+C завершает сервер.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.run()
    except KeyboardInterrupt:
        app.extensions["engine"].disconnect()
        server.close()
    finally:
        state_lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
