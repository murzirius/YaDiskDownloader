import json
import threading
import time
from pathlib import Path

import pytest
import requests

from conftest import Response, make_job, wait_job, test_timeout
from disk_downloader.api import APIError, Deadline, DiskAPI, NetworkError, Suspended
from disk_downloader.engine import Control
from disk_downloader.network import NetworkWait


def until(predicate, timeout=3):
    end = time.monotonic() + test_timeout(timeout)
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Ожидаемое событие не наступило")


def test_offline_warning_and_recovery_with_one_attempt_and_partial(system, monkeypatch):
    engine, api, _ = system
    content = b"verified original" * 500
    api.file("disk:/one.bin", data=content, preview=False)
    job_id = make_job(system, mode="fallback")
    online = threading.Event()
    resumed_request = threading.Event()
    monkeypatch.setattr(NetworkWait, "reachable", staticmethod(online.is_set))
    media_calls = []

    class Interrupted(Response):
        def iter_content(self, chunk_size):
            yield content[:100]
            raise requests.ConnectionError("DO NOT LEAK: token or signed URL")

    def media(path, kind, settings, checkpoint):
        media_calls.append((kind, settings.get("_range_offset", 0)))
        if len(media_calls) == 1:
            return Interrupted(content), None
        offset = settings.get("_range_offset", 0)
        assert offset == 100
        resumed_request.set()
        return Response(content[offset:], status=206, headers={"Content-Length": str(len(content) - offset), "Content-Range": f"bytes {offset}-{len(content)-1}/{len(content)}"}), None

    monkeypatch.setattr(api, "media", media)
    engine.action(job_id, "start", {"attempts": 1, "workers": 1, "timeout": 5, "create_zip": False})
    until(lambda: engine.status(job_id)["network_waiting"])
    control = engine.controls[job_id]
    control.network.interval = 0.05
    control.network.next_probe = 0
    job = engine.status(job_id)
    assert job["live"] and job["errors"] == 0 and job["speed"] == 0 and job["eta_seconds"] is None
    assert "Проверьте интернет" in job["network_warning"]
    assert job["partial_bytes"] == 100
    assert len(media_calls) == 1
    resumed = time.monotonic()
    online.set()
    assert resumed_request.wait(2), "Network recovery did not resume the request promptly"
    assert time.monotonic() - resumed < 2
    done = wait_job(engine, job_id)
    assert done["phase"] == "completed" and done["errors"] == 0 and not done["network_waiting"]
    assert media_calls == [("original", 0), ("original", 100)]
    row = next(engine.store.rows("SELECT original_attempts FROM entries WHERE job=? AND kind='file'", (job_id,)))
    assert row["original_attempts"] == 1
    artifact = next(engine.store.rows("SELECT local FROM artifacts WHERE job=?", (job_id,)))
    assert (Path(done["root"]) / artifact["local"]).read_bytes() == content
    logs = list(engine.store.rows("SELECT message,level FROM journal_events WHERE job=?", (job_id,)))
    assert sum("Нет связи" in row["message"] for row in logs) == 1
    assert sum("восстановлена" in row["message"] for row in logs) == 1
    assert all("DO NOT LEAK" not in row["message"] for row in logs)


def test_metadata_wait_keeps_page_and_attempt_count(system, monkeypatch):
    engine, fake, _ = system
    api = DiskAPI(fake.credentials)
    control = Control()
    online = threading.Event()
    control.network = NetworkWait(probe=online.is_set, interval=0.02)
    calls = []
    results = []

    def opened(url, params, auth, timeout, checkpoint, epoch):
        calls.append(params["offset"])
        if not online.is_set():
            raise NetworkError()
        return Response(json.dumps({"path": "disk:/", "type": "dir", "_embedded": {"items": []}}).encode()), None

    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    worker = threading.Thread(target=lambda: results.append(api.metadata("disk:/", {"attempts": 1, "timeout": 0.05}, control.check, offset=200)))
    worker.start()
    try:
        until(control.network.offline.is_set)
        time.sleep(0.12)
        assert calls == [200]
        online.set()
        worker.join(2)
        assert not worker.is_alive() and results[0]["path"] == "disk:/" and calls == [200, 200]
    finally:
        control.stop()
        worker.join(2)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_http_errors_do_not_claim_internet_is_down(status):
    gate = NetworkWait(probe=lambda: pytest.fail("HTTP error must not trigger connectivity probe"))
    assert not gate.recover(APIError("HTTP", status), lambda: None)
    assert not gate.offline.is_set()


def test_only_media_failure_with_healthy_api_consumes_normal_attempt():
    gate = NetworkWait(probe=lambda: True)
    assert not gate.recover(requests.ConnectionError(), lambda: None)
    assert not gate.offline.is_set()


def test_network_wait_is_excluded_from_statistics_clock():
    clock = [100]
    online = [False]
    gate = NetworkWait(probe=lambda: online[0], clock=lambda: clock[0])
    gate.inspect(force=True)
    clock[0] = 200
    assert gate.elapsed == 100
    online[0] = True
    gate.inspect(force=True)
    clock[0] = 220
    assert gate.elapsed == 100 and not gate.offline.is_set()


def test_manual_pause_survives_network_recovery_and_stop_is_prompt():
    control = Control()
    control.network = NetworkWait(probe=lambda: True, interval=0.01)
    control.network.offline.set()
    control.pause()
    errors = []

    def waiting():
        try:
            control.network.wait(control.check)
        except Suspended as error:
            errors.append(error)

    worker = threading.Thread(target=waiting)
    worker.start()
    time.sleep(0.1)
    assert control.paused and worker.is_alive()
    control.stop()
    worker.join(0.5)
    assert not worker.is_alive() and errors


def test_other_stream_deadline_is_aborted_when_connection_is_lost():
    control = Control()
    with Deadline(20, control.check, control.active_clock) as deadline:
        control.network.offline.set()
        until(deadline.network_aborted.is_set)
        with pytest.raises(NetworkError):
            deadline.check()
        assert not deadline.expired.is_set()


@pytest.mark.parametrize('_repeat',range(3))
def test_real_parallel_http_disconnect_resumes_without_old_socket_timeout(system, monkeypatch, _repeat):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit
    from disk_downloader import api as api_module
    engine, fake, _ = system
    bodies = {"disk:/a.bin": b"A" * 3000, "disk:/b.bin": b"B" * 4000}
    for path, data in bodies.items():
        fake.file(path, data=data, preview=False, media_type="other", mime_type="application/octet-stream")
    job_id = make_job(system, mode="fallback")
    online = threading.Event()
    online.set()
    first_started = threading.Event()
    release_old_socket = threading.Event()
    resumed_requests = {path: threading.Event() for path in bodies}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            if not online.is_set():
                self.close_connection = True
                return
            self.send_response(401)
            self.end_headers()

        def do_GET(self):
            if not online.is_set():
                self.close_connection = True
                return
            parsed = urlsplit(self.path)
            path = parse_qs(parsed.query).get("path", [""])[0]
            if parsed.path == "/resources":
                body = json.dumps(fake.tree[path]).encode()
            elif parsed.path == "/resources/download":
                body = json.dumps({"href": f"http://127.0.0.1:{self.server.server_port}/media?path={path}"}).encode()
            else:
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                calls.append((path, offset))
                first = len([item for item in calls if item[0] == path]) == 1
                if not first:
                    resumed_requests[path].set()
                if first and path == "disk:/b.bin":
                    assert first_started.wait(test_timeout(10))
                data = bodies[path]
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(data) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(data)-1}/{len(data)}")
                self.end_headers()
                try:
                    if first:
                        self.wfile.write(data[:100])
                        self.wfile.flush()
                        if path == "disk:/a.bin":
                            first_started.set()
                            release_old_socket.wait(10)
                        else:
                            online.clear()
                            self.close_connection = True
                    else:
                        self.wfile.write(data[offset:])
                except OSError:
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(api_module, "BASE", base)
    monkeypatch.setattr(api_module, "trusted_media", lambda url: url.startswith(base + "/"))
    monkeypatch.setattr(NetworkWait, "reachable", staticmethod(online.is_set))
    engine.api = DiskAPI(fake.credentials)
    try:
        engine.action(job_id, "start", {"attempts": 1, "workers": 2, "timeout": 20, "create_zip": False})
        until(lambda: engine.status(job_id)["network_waiting"])
        gate = engine.controls[job_id].network
        gate.interval = 0.02
        gate.next_probe = 0
        recovered = time.monotonic()
        online.set()
        for path, event in resumed_requests.items():
            if not event.wait(max(0, 3 - (time.monotonic() - recovered))):
                # Include blocked thread locations before teardown releases the
                # deliberately stalled server connection. No real data is used.
                import faulthandler
                faulthandler.dump_traceback()
                states=[{'finished':d.finished.is_set(),'aborted':d.network_aborted.is_set(),
                         'expired':d.expired.is_set(),'sockets':[sock.fileno() for sock in d.sockets]}
                        for d in tuple(gate.requests)]
                pytest.fail(f'Recovery request missing: {path}; calls={calls}; '
                            f'offline={gate.offline.is_set()}; requests={states}')
        assert time.monotonic() - recovered < 3
        done = wait_job(engine, job_id)
        assert done["errors"] == 0 and done["originals"] == 2 and done["phase"] == "completed"
        assert not release_old_socket.is_set()
        for row in engine.store.rows("SELECT path,local FROM artifacts WHERE job=?", (job_id,)):
            assert (Path(done["root"]) / row["local"]).read_bytes() == bodies[row["path"]]
    finally:
        engine.controls[job_id].stop()
        release_old_socket.set()
        server.shutdown()
        server.server_close()



def test_darwin_abort_keeps_buffered_reader_descriptor_until_owner_closes(monkeypatch):
    import socket
    from disk_downloader import api
    monkeypatch.setattr(api.sys,'platform','darwin')
    left,right=socket.socketpair()
    reader=left.makefile('rb')
    descriptor=left.fileno()
    deadline=Deadline(10)
    deadline.attach(sock=left)
    try:
        deadline.abort()
        assert left.fileno()==descriptor
        assert left.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE)==socket.SOCK_STREAM
    finally:
        reader.close();left.close();right.close()
    assert left.fileno()==-1
