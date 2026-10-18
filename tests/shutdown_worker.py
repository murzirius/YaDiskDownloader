"""Real server process for bounded exit regressions; uses only a fake Disk."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run
from disk_downloader import web,lifecycle
from conftest import FakeAPI
from disk_downloader.engine import Control
from disk_downloader.tray import Tray

folder, mode, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
original_create, original_shutdown = web.create_app, lifecycle.Shutdown


def create(state, port):
    api = FakeAPI()
    app = original_create(state, api=api, port=port)
    engine = app.extensions['engine']
    if mode == 'scan':
        api.file('disk:/one.bin', preview=False)
        original_metadata = api.metadata

        def metadata(*args, **kwargs):
            if kwargs.get('limit') == 200:
                while True:
                    args[2]()
                    time.sleep(.01)
            return original_metadata(*args, **kwargs)

        api.metadata = metadata
        job = engine.create({'destination': str(folder/'results'), 'whole_disk': True, 'auto_pause': False})
        (folder/'job').write_text(job)
    if mode == 'hung':
        from concurrent.futures import ThreadPoolExecutor

        def hung():
            with ThreadPoolExecutor(1) as pool:
                pool.submit(threading.Event().wait).result()

        engine.controls['hung'] = Control()
        worker = threading.Thread(target=hung, daemon=True)
        engine.threads['hung'] = worker
        worker.start()
    tray = Tray(engine, '', lambda: app.extensions['quit_app']())
    app.extensions['tray'] = tray

    @app.post('/api/test-tray-exit')
    def tray_exit():
        threading.Timer(.1, tray.exit).start()
        return {'ok': True}

    return app


web.create_app = create
lifecycle.Shutdown = lambda *args: original_shutdown(*args, grace=3, force_after=6)
sys.argv = ['run.py', '--port', str(port), '--state-dir', str(folder/'state'), '--no-tray', '--no-browser']
raise SystemExit(run.main())
