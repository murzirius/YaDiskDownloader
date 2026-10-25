"""Opt-in real Disk contract test. Reads only the dedicated fixtures folder.

The token is supplied through YADISK_TEST_TOKEN, never an argument or report.
Cloud content is never modified. Reports contain counters only.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from disk_downloader.api import APIError, Credentials, Deadline, DiskAPI
from disk_downloader.engine import Engine
from disk_downloader.paths import remote_path, safe_join
from disk_downloader.store import Store

PREFIX = 'disk:/YaDiskDownloader-tests'
MAX_BYTES = 64 * 1024 * 1024
MAX_REQUESTS = 500


def validate_manifest(value):
    if not isinstance(value, dict) or value.get('format') != 1:
        raise ValueError('Invalid fixture manifest')
    files = value.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 100:
        raise ValueError('Fixture count must be 1..100')
    names, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
            raise ValueError('Invalid fixture record')
        path = item['path']
        if not isinstance(path, str) or not path or len(path) > 1000 or '\\' in path or ':' in path or '\0' in path:
            raise ValueError('Invalid fixture path')
        if PurePosixPath(path).is_absolute() or any(part in ('', '.', '..') for part in path.split('/')) or path.casefold() in names:
            raise ValueError('Invalid or repeated fixture path')
        names.add(path.casefold())
        if type(item['size']) is not int or not 0 <= item['size'] <= MAX_BYTES:
            raise ValueError('Invalid fixture size')
        if not isinstance(item['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['sha256']):
            raise ValueError('Invalid fixture hash')
        total += item['size']
    if total > MAX_BYTES:
        raise ValueError('Fixtures exceed 64 MB')
    return files


class TestAPI(DiskAPI):
    def __init__(self, credentials):
        super().__init__(credentials)
        self.test_lock = threading.Lock()
        self.test_requests = 0
        self.next_request = 0
        self.allowed_files = None
        self.inject_break = False
        self.break_injected = self.range_requested = self.range_supported = False

    def _open(self, *args, **kwargs):
        checkpoint = args[4]
        with self.test_lock:
            self.test_requests += 1
            if self.test_requests > MAX_REQUESTS:
                raise APIError('Test request budget exceeded')
            wait = max(0, self.next_request - time.monotonic())
            self.next_request = max(time.monotonic(), self.next_request) + 0.3
        self.wait(wait, checkpoint)
        return super()._open(*args, **kwargs)

    def metadata(self, path, *args, **kwargs):
        if path != PREFIX and not path.startswith(PREFIX + '/'):
            raise ValueError('Read outside the fixture folder is forbidden')
        result = super().metadata(path, *args, **kwargs)
        if self.allowed_files is not None:
            candidates = [result] if result.get('type') == 'file' else result.get('_embedded', {}).get('items', [])
            if any(item.get('type') == 'file' and item.get('path') not in self.allowed_files for item in candidates):
                raise APIError('The fixture folder contains an unexpected file. Test download refused.')
        return result

    def media(self, path, kind, settings, checkpoint=lambda: None):
        if path != PREFIX and not path.startswith(PREFIX + '/'):
            raise ValueError('Read outside the fixture folder is forbidden')
        response, connection = super().media(path, kind, settings, checkpoint)
        if settings.get('_range_offset'):
            self.range_requested = True
            self.range_supported |= response.status_code == 206
        if self.inject_break and not self.break_injected and kind == 'original' and path.endswith('/resume-test.bin'):
            self.break_injected = True
            original = response.iter_content
            def interrupted(chunk_size):
                received = 0
                for chunk in original(chunk_size):
                    yield chunk
                    received += len(chunk)
                    if received >= 512 * 1024:
                        raise APIError('Synthetic connection interruption')
            response.iter_content = interrupted
        return response, connection


def read_manifest(api):
    settings = {'attempts':1, 'timeout':20, 'total_timeout':30}
    with Deadline(30) as deadline:
        response, connection = api.media(PREFIX + '/fixture-manifest.json', 'original', settings | {'_deadline':deadline}, deadline.check)
        try:
            if response.status_code != 200:
                api.fail_response(response)
            data = bytearray()
            for chunk in response.iter_content(65536):
                deadline.check()
                data.extend(chunk)
                if len(data) > 64 * 1024:
                    raise ValueError('Fixture manifest exceeds 64 KB')
            return validate_manifest(json.loads(data))
        finally:
            api.close(response, connection)


def wait_job(engine, job):
    worker = engine.threads[job]
    until = time.monotonic() + 600
    while worker.is_alive():
        worker.join(0.2)
        control = engine.controls[job]
        if control.paused or time.monotonic() > until:
            control.stop()
            worker.join(10)
            raise AssertionError('Fixture job paused or exceeded 10 minutes')
    status = engine.status(job)
    if status['phase'] != 'completed':
        raise AssertionError('Fixture job did not complete')
    return status


def check_downloads(engine, job, files):
    info = engine.store.job(job)
    with engine.store.db() as db:
        rows = db.execute("SELECT path,local,size,sha256 FROM artifacts WHERE job=? AND type='original'", (job,)).fetchall()
    actual = {row['path']: row for row in rows}
    for expected in files:
        row = actual[PREFIX + '/' + expected['path']]
        filename = safe_join(info['root'], row['local'])
        with open(filename, 'rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        if row['size'] != expected['size'] or sha != expected['sha256']:
            raise AssertionError('Fixture integrity mismatch')


def run(token):
    credentials = Credentials();credentials.set(token)
    api = TestAPI(credentials)
    engine = None
    temporary = None
    started = time.monotonic()
    try:
        files = read_manifest(api)
        api.allowed_files = {PREFIX + '/' + f['path'] for f in files} | {PREFIX + '/fixture-manifest.json'}
        temporary = tempfile.TemporaryDirectory(prefix='yadisk-live-')
        folder = temporary.name
        root = Path(folder)
        engine = Engine(Store(root / 'state/jobs.sqlite3'), api)
        engine.traffic.configure(64)
        # Explicit fixture paths prevent an accidentally uploaded private
        # file from joining the download, even if the remote folder changes.
        settings = dict(destination=str(root / 'downloads'), paths=[PREFIX], exclusions=[PREFIX + '/fixture-manifest.json'],
                        mode='fallback', auto_start=True, workers=2, attempts=2,
                        timeout=30, total_timeout=120, reports=['json'], create_zip=False)
        api.inject_break = True
        job = engine.create(settings)
        wait_job(engine, job);check_downloads(engine, job, files)
        before = engine.traffic.snapshot()['media_bytes']
        missing = engine.create(settings | {'operation':'missing', 'destination':engine.store.job(job)['root']})
        wait_job(engine, missing);check_downloads(engine, missing, files)
        if engine.traffic.snapshot()['media_bytes'] != before:
            raise AssertionError('Missing download requested already verified originals')
        # Preview checks have a separate small bounded read.
        preview = next((f for f in files if f['path'].lower().endswith('.jpg')), None)
        preview_ok = False
        if preview:
            with Deadline(30) as deadline:
                response, conn = api.media(PREFIX+'/'+preview['path'], 'preview', {'preview_size':'M','attempts':1,'timeout':30,'_deadline':deadline}, deadline.check)
                try:
                    if response.status_code != 200:
                        api.fail_response(response)
                    data=bytearray()
                    for chunk in response.iter_content(65536):
                        deadline.check();data.extend(chunk)
                        if len(data)>4*1024*1024:raise ValueError('Preview exceeds test budget')
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                    preview_ok=True
                finally:
                    api.close(response,conn)
        if any(f['path']=='resume-test.bin' for f in files) and not (api.break_injected and api.range_requested):
            raise AssertionError('Partial resume was not exercised')
        return dict(status='passed', files=len(files), sha256_ok=True, scan_ok=True, missing_ok=True,
                    preview_ok=preview_ok, interruption_injected=api.break_injected,
                    range_requested=api.range_requested, range_supported=api.range_supported,
                    requests=api.test_requests, payload_bytes=engine.traffic.snapshot()['bytes'],
                    seconds=round(time.monotonic()-started,2))
    finally:
        if engine:
            for control in engine.controls.values():control.stop()
            for worker in engine.threads.values():worker.join(10)
        credentials.clear()
        api.json_connections.close_idle()
        if temporary:
            temporary.cleanup()


def main():
    parser=argparse.ArgumentParser(description='Read-only real Yandex Disk integration check')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    token=os.environ.pop('YADISK_TEST_TOKEN','')
    if not token:
        result={'status':'not_configured','message':'Configure the YADISK_TEST_TOKEN environment secret.'}
        code=2
    else:
        try:
            result=run(token);code=0
        except APIError as exc:
            result={'status':'external_error','source':'Yandex Disk or internet','http_status':exc.status};code=2
        except Exception as exc:
            # Exception strings may include response URLs or local paths.
            result={'status':'failed','error_type':type(exc).__name__};code=1
        finally:
            token=None
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    return code


if __name__=='__main__':
    raise SystemExit(main())
