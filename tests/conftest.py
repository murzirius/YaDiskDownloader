import copy
import hashlib
import io
import threading
import time
import shutil
import os
import sys
import traceback

import pytest
from PIL import Image

from disk_downloader.api import APIError, Credentials, DiskAPI
from disk_downloader.engine import Engine
from disk_downloader.paths import parent_path
from disk_downloader.store import Store


def test_timeout(seconds):
    """Scale test scheduling waits, never application request deadlines."""
    factor = float(os.environ.get("YADISK_TEST_TIMEOUT_FACTOR", "1"))
    if not 1 <= factor <= 10:
        raise ValueError("YADISK_TEST_TIMEOUT_FACTOR must be between 1 and 10")
    return seconds * factor


test_timeout.__test__ = False


def stop_engines(engines):
    for engine in engines:
        engine.shutting_down = True
        for control in tuple(engine.controls.values()):
            control.stop()
    deadline = time.monotonic() + test_timeout(15)
    remaining = []
    for engine in engines:
        for worker in tuple(engine.threads.values()):
            if worker is not threading.current_thread() and worker.is_alive():
                worker.join(max(0, deadline - time.monotonic()))
            if worker.is_alive():
                remaining.append(worker.name)
    if remaining:
        raise AssertionError("Test left active job threads: " + ", ".join(remaining))


@pytest.fixture(autouse=True)
def cleanup_test_engines(monkeypatch):
    engines = []
    original = Engine.__init__

    def tracked(engine, *args, **kwargs):
        original(engine, *args, **kwargs)
        engines.append(engine)

    monkeypatch.setattr(Engine, "__init__", tracked)
    try:
        yield
    finally:
        stop_engines(engines)


@pytest.fixture(autouse=True)
def simulated_free_space(monkeypatch):
    original = shutil.disk_usage
    sample = original(".")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(sample)(sample.total, sample.total // 2, sample.total - sample.total // 2))
    monkeypatch.setattr("disk_downloader.updates.Updates.start", lambda self, force=False: None)


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (20, 12), (10, 30, 60)).save(out, "PNG")
    return out.getvalue()


class Response:
    def __init__(self, data, status=200, headers=None, chunks=7):
        self.data = data
        self.status_code = status
        self.headers = headers or {"Content-Length": str(len(data))}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.data), self.chunks):
            yield self.data[offset:offset + self.chunks]

    def close(self):
        self.closed = True


class FakeAPI:
    def __init__(self):
        self.credentials = Credentials()
        self.credentials.set("fake-token-for-tests")
        self.tree = {"disk:/": {"path": "disk:/", "name": "Диск", "type": "dir"}}
        self.data = {}
        self.fail = {}
        self.metadata_calls = []
        self.media_calls = []
        self.block = None
        self.delay = 0
        self.lock = threading.Lock()

    def directory(self, path):
        if parent_path(path) not in self.tree:
            self.directory(parent_path(path))
        self.tree[path] = {"path": path, "name": path.rsplit("/", 1)[-1], "type": "dir"}

    def file(self, path, data=b"original", preview=True, media_type="image", mime_type="image/png"):
        if parent_path(path) not in self.tree:
            self.directory(parent_path(path))
        meta = {"path": path, "name": path.rsplit("/", 1)[-1], "type": "file", "size": len(data), "md5": hashlib.md5(data).hexdigest(), "sha256": hashlib.sha256(data).hexdigest(), "modified": "2026-10-13T00:00:00Z", "resource_id": path}
        meta.update(media_type=media_type, mime_type=mime_type)
        if preview:
            meta["preview"] = "https://downloader.disk.yandex.ru/preview/transient-link"
            self.data[(path, "preview")] = image_bytes()
        self.tree[path] = meta
        self.data[(path, "original")] = data

    def metadata(self, path, settings=None, checkpoint=lambda: None, offset=0, limit=200, **kwargs):
        checkpoint()
        if not self.credentials.connected:
            raise APIError("Не подключён", 401)
        self.metadata_calls.append((path, offset, limit))
        if path not in self.tree:
            raise APIError("Ресурс недоступен", 404)
        result = copy.deepcopy(self.tree[path])
        if result["type"] == "dir":
            children = sorted((v for k, v in self.tree.items() if k != "disk:/" and parent_path(k) == path), key=lambda v: v["name"])
            result["_embedded"] = {"items": copy.deepcopy(children[offset:offset + limit]), "total": len(children)}
        return result

    def media(self, path, kind, settings, checkpoint=lambda: None):
        checkpoint()
        with self.lock:
            self.media_calls.append((path, kind))
            failures = self.fail.get((path, kind), [])
            if failures:
                error = failures.pop(0)
                raise error
        if self.block:
            self.block.set()
            while self.delay:
                checkpoint()
                time.sleep(0.01)
        if (path, kind) not in self.data:
            raise APIError("Превью отсутствует", 404)
        return Response(self.data[(path, kind)]), None

    def wait(self, delay, checkpoint):
        checkpoint()

    def close(self, response, session):
        response.close()


def wait_job(engine, job_id, phases=None, timeout=30):
    until = time.monotonic() + test_timeout(timeout)
    while time.monotonic() < until:
        worker = engine.threads.get(job_id)
        if phases and engine.store.job(job_id)["phase"] in phases:
            return engine.status(job_id)
        if not phases and (worker is None or not worker.is_alive()):
            return engine.status(job_id)
        delay = min(0.05, max(0, until - time.monotonic()))
        if worker and worker.is_alive():
            worker.join(delay)
        else:
            time.sleep(delay)
    worker = engine.threads.get(job_id)
    frame = sys._current_frames().get(worker.ident) if worker else None
    stack = "".join(traceback.format_stack(frame)) if frame else "No active worker stack"
    raise AssertionError("Задание не завершилось вовремя: " + str(engine.status(job_id)) + "\nWorker stack:\n" + stack)


@pytest.fixture
def system(tmp_path):
    api = FakeAPI()
    store = Store(tmp_path / "state" / "jobs.sqlite3")
    engine = Engine(store, api)
    return engine, api, tmp_path


def make_job(system, mode="previews", paths=None, whole=False, reports=None):
    engine, api, tmp = system
    job_id = engine.create({"destination": str(tmp / "downloads"), "paths": paths or ["disk:/"], "whole_disk": whole, "mode": mode, "reports": reports or ["csv", "json", "html", "txt"], "retries": 1, "workers": 2})
    job = wait_job(engine, job_id)
    assert job["phase"] == "ready", job
    return job_id


def run_job(system, **kwargs):
    job_id = make_job(system, **kwargs)
    engine = system[0]
    engine.action(job_id, "start")
    return wait_job(engine, job_id)
