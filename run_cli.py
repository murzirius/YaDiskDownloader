"""Console entry; a detached worker uses the same packaged application."""
import sys

if '--serve' in sys.argv:
    sys.argv.remove('--serve')
    from run import main
else:
    from disk_downloader.cli import main

raise SystemExit(main())
