"""Console entry; a detached worker uses the same packaged application."""
import sys

if sys.platform != 'linux':
    print('CLI доступен только в Linux. На Windows и macOS откройте приложение.', file=sys.stderr)
    raise SystemExit(2)

if '--serve' in sys.argv:
    sys.argv.remove('--serve')
    from run import main
else:
    from disk_downloader.cli import main

raise SystemExit(main())
