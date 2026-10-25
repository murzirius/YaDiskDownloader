"""Serve disposable synthetic jobs for check_web_layout.cjs, without real tokens."""
import argparse
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from conftest import FakeAPI, stop_engines, wait_job
from disk_downloader.service_outputs import root_key
from disk_downloader.updates import Updates
from disk_downloader.web import create_app
from flask import jsonify, send_file
from waitress import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    Updates.start = lambda *args, **kwargs: None
    sample = shutil.disk_usage('.')
    shutil.disk_usage = lambda path: type(sample)(sample.total, sample.total // 2, sample.total - sample.total // 2)
    api = FakeAPI()
    for folder in ('Фото', 'Очень длинное название семейного архива с фотографиями и видеозаписями 2026 года'):
        for index in range(4):
            api.file('disk:/' + folder + '/Снимок с длинным названием ' + str(index) + '.jpg',
                     b'checked photo ' + bytes([index]), preview=True)
    for name, mime in [('Видео.mp4', 'video/mp4'), ('Музыка.flac', 'audio/flac'),
                       ('Документ.pdf', 'application/pdf'), ('Архив.zip', 'application/zip')]:
        api.file('disk:/' + name, b'content', preview=False, mime_type=mime, media_type='unknown')
    api.directory('disk:/Пустая папка')
    with tempfile.TemporaryDirectory(prefix='yadisk-layout-') as folder:
        root = Path(folder)
        app = create_app(root / 'state', api=api, port=args.port)
        app.config['TEMPLATES_AUTO_RELOAD'] = True
        engine = app.extensions['engine']
        server = None
        try:
            completed = engine.create({'destination': str(root / 'results'), 'whole_disk': True,
                'mode': 'fallback', 'reports': ['csv', 'json', 'html', 'txt'],
                'create_zip': False, 'auto_start': True})
            assert wait_job(engine, completed, timeout=40)['phase'] == 'completed'
            ready = engine.create({'destination': str(root / 'next'), 'whole_disk': True,
                'mode': 'both', 'reports': ['json'], 'create_zip': False, 'auto_start': False})
            assert wait_job(engine, ready, timeout=40)['phase'] == 'ready'
            engine.store.update_job(completed, title='Семейные фото — очень длинное название завершённой загрузки')
            engine.store.update_job(ready, title='Подготовленная загрузка — оригиналы и превью')

            @app.get('/qa/state')
            def qa_state():
                return jsonify(synthetic_layout_fixture=True, completed=completed, ready=ready)

            def output(kind):
                job = engine.store.job(completed)
                with engine.store.db() as db:
                    row = db.execute('SELECT local FROM service_outputs WHERE root=? AND kind=?',
                                     (root_key(job['root']), kind)).fetchone()
                assert row is not None, kind
                return send_file(Path(job['root']) / row[0])

            @app.get('/qa/journal')
            def qa_journal():
                return output('journal')

            @app.get('/qa/changes')
            def qa_changes():
                return output('changes')

            @app.get('/qa/report')
            def qa_report():
                return output('report:html')

            @app.post('/qa/stop')
            def qa_stop():
                threading.Thread(target=server.close, daemon=True).start()
                return jsonify(stopping=True)

            # Exercise the real Exit control without attaching to a user session.
            app.extensions['quit_app'] = lambda: threading.Thread(target=server.close, daemon=True).start()
            server = create_server(app, host='127.0.0.1', port=args.port, threads=8)
            print(f'Synthetic layout fixture: http://127.0.0.1:{args.port}', flush=True)
            server.run()
        except KeyboardInterrupt:
            pass
        finally:
            if server:
                server.close()
            stop_engines([engine])


if __name__ == '__main__':
    main()
