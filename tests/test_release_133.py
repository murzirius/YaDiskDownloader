import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FakeAPI, make_job, wait_job
from disk_downloader.api import APIError, Deadline, DiskAPI
from disk_downloader.sqlite_support import safe_wal, require_safe


@pytest.mark.parametrize('version,safe', [((3,50,4),False), ((3,50,7),True),
    ((3,51,2),False), ((3,51,3),True), ((3,52,0),True),
    ((3,44,5),False), ((3,44,6),True), ((3,45,1),False)])
def test_sqlite_patched_branches(version, safe):
    assert safe_wal(version) is safe


def test_release_gate_rejects_old_sqlite(monkeypatch):
    monkeypatch.setattr('disk_downloader.sqlite_support.safe_wal', lambda: False)
    with pytest.raises(RuntimeError, match='WAL-reset'):
        require_safe()


def test_source_legacy_database_uses_safe_rollback_journal(tmp_path, monkeypatch):
    from disk_downloader.store import Store
    monkeypatch.setattr('disk_downloader.sqlite_support.safe_wal', lambda: False)
    store = Store(tmp_path/'history.sqlite3')
    with store.db() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2


def test_legacy_stream_snapshot_allows_writing_while_consuming_rows(tmp_path, monkeypatch):
    from disk_downloader.store import Store
    monkeypatch.setattr('disk_downloader.sqlite_support.safe_wal', lambda: False)
    store = Store(tmp_path/'history.sqlite3')
    with store.db() as db:
        db.executemany('INSERT INTO preferences VALUES(?,?)', ((str(n),'before') for n in range(450)))
    iterator = store.rows('SELECT key,value FROM preferences ORDER BY CAST(key AS INTEGER)')
    first = next(iterator)
    done = threading.Event()
    errors = []
    def writer():
        try:
            with store.db() as db:
                db.execute("UPDATE preferences SET value='after'")
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert done.wait(5), 'Rollback reader blocked a writer while yielding rows'
        assert not errors
        snapshot = [first,*iterator]
        assert len(snapshot) == 450 and all(row['value']=='before' for row in snapshot)
        assert next(store.rows('SELECT value FROM preferences'))['value'] == 'after'
    finally:
        iterator.close()
        thread.join(10)


def test_idle_clock_progress_hold_and_independent_total():
    now = [0.0]
    deadline = Deadline(100, clock=lambda: now[0], idle_seconds=20)
    now[0] = 15
    deadline.touch()
    now[0] = 30
    assert deadline.remaining() == 5
    with deadline.hold():
        now[0] = 80
        assert deadline.remaining() == 5
    deadline.touch()
    now[0] = 149
    deadline.touch()
    assert deadline.remaining() == 1  # Progress never resets the total limit.
    now[0] = 151
    with pytest.raises(APIError, match='лимит попытки'):
        deadline.check()


@pytest.mark.parametrize('mode', ['progress', 'stalled', 'total'])
def test_real_socket_idle_budget_reads_below_iter_content_buffer(monkeypatch, mode):
    # The entire response is smaller than iter_content's 64 KiB buffer. Counting
    # yielded chunks alone would incorrectly time out a live, slow download.
    stop = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            data = b'{"value":"' + b'x'*20 + b'"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                for byte in data:
                    if stop.wait(.07):
                        return
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if mode == 'stalled' and stop.wait(2):
                        return
            except OSError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr('disk_downloader.api.BASE', f'http://127.0.0.1:{server.server_port}')
    api = DiskAPI(FakeAPI().credentials)
    try:
        with Deadline(.8 if mode == 'total' else 0, idle_seconds=.7) as deadline:
            settings = {'attempts':1, 'timeout':20, '_deadline':deadline}
            if mode == 'progress':
                assert api.get('/resources', settings=settings) == {'value':'x'*20}
            else:
                with pytest.raises(APIError):
                    api.get('/resources', settings=settings)
                assert deadline.expired.is_set() or deadline.remaining() < .1
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_completeness_counts_files_once_and_keeps_fallback_out_of_errors(system):
    engine, api, _ = system
    for name in ('original', 'preview', 'missing', 'both'):
        api.file('disk:/'+name+'.png')
    api.fail[('disk:/preview.png','original')] = [APIError('blocked', 403)]*9
    api.fail[('disk:/missing.png','original')] = [APIError('blocked', 403)]*9
    api.fail[('disk:/missing.png','preview')] = [APIError('blocked', 403)]*9
    job = make_job(system, mode='both')
    engine.action(job, 'start')
    result = wait_job(engine, job)
    assert result['completeness'] == {'selected':4,'originals':2,'preview_only':1,'no_result':1}
    assert result['errors'] == 1
    from disk_downloader.reports import context
    assert context(engine.store, job)['completeness'] == result['completeness']


def test_large_file_timeouts_validate_and_survive_resume(system):
    engine, api, _ = system
    api.file('disk:/large.bin', preview=False)
    job = make_job(system)
    engine.action(job, 'start', {'timeout':1200, 'total_timeout':36000})
    result = wait_job(engine, job)
    assert result['settings']['timeout'] == 1200
    assert result['settings']['total_timeout'] == 36000
    for invalid in (-1, 86401, True, 1.5):
        with pytest.raises(ValueError):
            engine.validate_settings(result['settings'] | {'total_timeout':invalid})
