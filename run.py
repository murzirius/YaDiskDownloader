import argparse
import os
import socket
import sys
import threading
import webbrowser
import json
import re
from pathlib import Path

from disk_downloader.runtime import frozen, state_folder, program_folder
from disk_downloader import desktop, startup


def message(text):
    if sys.stdout:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
            print(text.encode(encoding, errors='backslashreplace').decode(encoding), flush=True)
    elif os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "YaDiskDownloader", 0x40)


def lock_state(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    startup.mark('imports')
    desktop.progress('Загружаем компоненты приложения…',10)
    from waitress import create_server
    from disk_downloader.web import create_app
    from disk_downloader.migration import migrate, candidates
    from disk_downloader.tray import Tray
    from disk_downloader.lifecycle import Shutdown
    parser = argparse.ArgumentParser(description="Локальное скачивание Яндекс.Диска")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument('--window',action='store_true',help='Показать мини-окно состояния Windows')
    parser.add_argument('--no-window',action='store_true',help='Запустить без мини-окна')
    parser.add_argument("--state-dir")
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
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
    startup.mark('state')
    desktop.progress('Проверяем порт и папку состояния…',25)
    state = Path(args.state_dir) if args.state_dir else state_folder()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
    except OSError:
        listener.close()
        from disk_downloader.single_instance import activate_existing
        if activate_existing(state,args.port):return 0
        message(f"Порт {args.port} занят. Если приложение уже запущено, откройте его через значок в трее. Для отдельного запуска выберите --port.")
        return 1
    try:
        state_lock = lock_state(state)
    except OSError:
        listener.close()
        message("Папка состояния занята другим процессом или недоступна. Завершите его или выберите отдельную --state-dir.")
        return 1
    recovery = desktop.confirm_history_recovery if desktop.status()['enabled'] else None
    history_progress = desktop.history_progress if desktop.status()['enabled'] else None
    startup.mark('history')
    desktop.progress('Сохраняем резервную копию и готовим историю…',40)
    migration = migrate(state, candidates(program_folder()), checkpoint=startup.checkpoint, recovery=recovery, progress=history_progress) if not args.state_dir else {"jobs": 0, "failed_sources": 0}
    startup.mark('database')
    desktop.progress('Открываем базу и восстанавливаем состояние…',60)
    app = create_app(state, port=args.port, startup_checkpoint=startup.checkpoint, startup_recovery=recovery, startup_progress=history_progress)
    startup.mark('server')
    app.extensions["migration"] = migration
    server = create_server(app, sockets=[listener], threads=12, clear_untrusted_proxy_headers=True)
    url = f"http://127.0.0.1:{args.port}"
    if sys.stdout:
        message(f"Приложение доступно: {url}")
    shutdown = Shutdown(app, server, state_lock)
    if os.name != 'nt' and threading.current_thread() is threading.main_thread():
        import signal
        signal.signal(signal.SIGTERM, lambda *_: shutdown())
    app.extensions["quit_app"] = shutdown
    app.extensions['management'].start()
    desktop.progress('Запускаем локальный сервер и управление…',85)
    if os.name == "nt" and not args.no_tray:
        try:
            tray = Tray(app.extensions["engine"], url, shutdown)
            app.extensions["tray"] = tray
            tray.start()
        except Exception:
            message("Не удалось создать значок в трее. Завершение доступно в меню «Обновления и диагностика».")
    if not args.no_browser and not desktop._window:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if args.update_ack:
        from disk_downloader import __version__
        ack = Path(args.update_ack).resolve()
        if ack.name != 'ready.json' or not re.fullmatch('[a-f0-9]{32}', ack.parent.name) or ack.parent.parent != (state.resolve() / 'updates'):
            raise ValueError('Некорректный путь подтверждения обновления')
        with ack.open('x', encoding='utf-8') as stream:
            json.dump({'version':__version__}, stream)
            stream.flush(); os.fsync(stream.fileno())
    def activate():
        if not desktop.show():
            threading.Thread(target=webbrowser.open,args=(url,),daemon=True,name='disk-open-browser').start()
    app.extensions['activate_window'] = activate
    desktop.ready(app,url,shutdown)
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
        startup.reset()
        window = os.name=='nt' and '--no-window' not in sys.argv and '--apply-update' not in sys.argv and ('--window' in sys.argv or frozen() and '--no-browser' not in sys.argv)
        sys.exit(desktop.launch(main) if window else main())
    except Exception as exc:
        if frozen():
            detail = startup.failure(exc)
            message(detail["message"] + "\nКод: " + detail["code"] + ("\nДиагностика: " + str(detail["report"]) if detail["report"] else ""))
            sys.exit(1)
        raise
