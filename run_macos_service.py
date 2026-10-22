"""Private bundled macOS worker entry point."""
import sys


def main():
    if sys.platform != 'darwin':
        print('Этот служебный процесс используется только приложением macOS.', file=sys.stderr)
        return 2
    if '--serve' in sys.argv:
        sys.argv.remove('--serve')
        from run import main as serve
        return serve()
    from disk_downloader.macos_service import main as control
    return control()


if __name__ == '__main__':
    raise SystemExit(main())
