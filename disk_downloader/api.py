import threading
import socket
import time
from contextlib import nullcontext, contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from .paths import remote_path


BASE = "https://cloud-api.yandex.net/v1/disk"
SIZES = ("S", "M", "L", "XL", "XXL", "XXXL")


class Suspended(Exception):
    pass


class APIError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NetworkError(APIError):
    def __init__(self, message="Сетевая ошибка или таймаут", offline=False):
        super().__init__(message)
        self.offline = offline


def attempt_count(settings):
    return settings.get("attempts", settings.get("retries", 0) + 1)


class Deadline:
    def __init__(self, seconds, checkpoint=lambda: None, clock=time.monotonic, cancelled=lambda: False):
        self.seconds = seconds
        self.checkpoint = checkpoint
        self.clock = clock
        self.cancelled = cancelled
        self.network = getattr(getattr(checkpoint, "__self__", None), "network", None)
        self.network_aborted = threading.Event()
        self.started = clock()
        self.held = 0
        self.hold_started = None
        self.finished = threading.Event()
        self.expired = threading.Event()
        self.lock = threading.Lock()
        self.connections = []
        self.sockets = []
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def __enter__(self):
        if self.network:
            self.network.register(self)
        self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        self.finished.set()
        if self.network:
            self.network.unregister(self)
        self.abort()
        self.thread.join(0.2)

    def remaining(self):
        current = self.clock()
        return max(0, self.seconds - ((self.hold_started if self.hold_started is not None else current) - self.started - self.held))

    @contextmanager
    def hold(self):
        self.hold_started = self.clock()
        try:
            yield
        finally:
            self.held += self.clock() - self.hold_started
            self.hold_started = None

    def check(self):
        self.checkpoint()
        if self.network_aborted.is_set() or self.network and self.network.offline.is_set():
            raise NetworkError(offline=True)
        if self.expired.is_set() or self.remaining() <= 0:
            self.expired.set()
            raise NetworkError(f"Превышен лимит попытки: {self.seconds} с")

    def attach(self, connection=None, response=None):
        with self.lock:
            if connection is not None:
                self.connections.append(connection)
            if response is not None:
                raw = getattr(response, "raw", None)
                fp = getattr(getattr(raw, "_fp", None), "fp", None)
                sock = getattr(getattr(fp, "raw", None), "_sock", None)
                if sock is not None:
                    self.sockets.append(sock)
        self.check()

    def abort(self):
        with self.lock:
            sockets = list(self.sockets) + [getattr(conn, "sock", None) for conn in self.connections]
        for sock in sockets:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    descriptor = sock.detach()
                    if descriptor != -1:
                        socket.close(descriptor)
                except OSError:
                    pass

    def watch(self):
        while not self.finished.wait(0.025):
            if self.network_aborted.is_set() or self.network and self.network.offline.is_set():
                self.network_aborted.set()
                self.abort()
                continue
            if self.cancelled() or self.remaining() <= 0:
                self.expired.set()
                self.abort()


class DeadlineAdapter(HTTPAdapter):
    def __init__(self, deadline):
        self.deadline = deadline
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        super().init_poolmanager(connections, maxsize, block, **kwargs)
        deadline = self.deadline

        class TimedHTTPPool(HTTPConnectionPool):
            def _new_conn(self):
                conn = super()._new_conn()
                deadline.attach(connection=conn)
                return conn

        class TimedHTTPSPool(HTTPSConnectionPool):
            def _new_conn(self):
                conn = super()._new_conn()
                deadline.attach(connection=conn)
                return conn

        self.poolmanager.pool_classes_by_scheme = {"http": TimedHTTPPool, "https": TimedHTTPSPool}


def retry_delay(value, attempt):
    if value:
        try:
            return max(0, float(value))
        except (ValueError, TypeError):
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return min(30, 0.5 * 2 ** attempt)


def trusted_media(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = ("disk.yandex.ru", "disk.yandex.net", "disk.yandex.com", "storage.yandex.net", "storage.yandex.ru")
        valid = host == "cloud-api.yandex.net" or any(host == domain or host.endswith("." + domain) for domain in allowed)
        return parsed.scheme == "https" and valid and parsed.port in (None, 443) and not parsed.username and not parsed.password
    except (ValueError, TypeError):
        return False


class Credentials:
    def __init__(self):
        self.lock = threading.RLock()
        self.token = None
        self.epoch = 0
        self.responses = set()

    def set(self, token):
        if not isinstance(token, str) or not 10 <= len(token) <= 4096 or any(c.isspace() or ord(c) < 32 for c in token):
            raise ValueError("Некорректный OAuth-токен")
        self.clear()
        with self.lock:
            self.token = token

    def clear(self):
        with self.lock:
            self.token = None
            self.epoch += 1
            active = list(self.responses)
            self.responses.clear()
        for response in active:
            response.close()

    def snapshot(self):
        with self.lock:
            if not self.token:
                raise Suspended("Подключите Диск")
            return self.token, self.epoch

    def check(self, epoch):
        with self.lock:
            if not self.token or self.epoch != epoch:
                raise Suspended("Диск отключён")

    @property
    def connected(self):
        with self.lock:
            return bool(self.token)


class DiskAPI:
    def __init__(self, credentials):
        self.credentials = credentials
        self.rate_lock = threading.Lock()
        self.rate_until = 0

    def wait(self, delay, checkpoint):
        until = time.monotonic() + delay
        while time.monotonic() < until:
            checkpoint()
            time.sleep(min(0.2, max(0, until - time.monotonic())))
        checkpoint()

    def _open(self, url, params, auth, timeout, checkpoint, epoch, extra_headers=None):
        self.credentials.check(epoch)
        checkpoint()
        with self.rate_lock:
            delay = max(0, self.rate_until - time.monotonic())
        self.wait(delay, checkpoint)
        token, current_epoch = self.credentials.snapshot()
        if current_epoch != epoch:
            raise Suspended("Подключение изменилось")
        session = requests.Session()
        response = None
        session.trust_env = False
        deadline = getattr(checkpoint, "deadline", None)
        if deadline:
            session.mount("https://", DeadlineAdapter(deadline))
            session.mount("http://", DeadlineAdapter(deadline))
        headers = {"Accept-Encoding": "identity", "User-Agent": "DiskLocalDownloader/1.0"}
        if extra_headers:
            headers.update({key: value for key, value in extra_headers.items() if key in ("Range", "If-Range")})
        if auth:
            headers["Authorization"] = "OAuth " + token
        token = None
        try:
            remaining = deadline.remaining() if deadline else timeout
            checkpoint()
            response = session.get(url, params=params, headers=headers, timeout=(max(0.01, min(10, remaining / 2)), max(0.01, remaining)), allow_redirects=False, stream=True)
            if deadline:
                deadline.attach(response=response)
            self.credentials.check(epoch)
            with self.credentials.lock:
                self.credentials.responses.add(response)
            return response, session
        except requests.RequestException:
            if response is not None:
                response.close()
            session.close()
            if deadline:
                if deadline.network_aborted.is_set():
                    raise NetworkError(offline=True) from None
                if deadline.expired.is_set():
                    raise NetworkError(f"Превышен лимит попытки: {timeout} с") from None
                deadline.check()
            raise NetworkError() from None
        except BaseException:
            if response is not None:
                response.close()
            session.close()
            raise
        finally:
            headers.clear()

    def close(self, response, session):
        with self.credentials.lock:
            self.credentials.responses.discard(response)
        response.close()
        session.close()

    def fail_response(self, response, attempt=0):
        status = response.status_code
        delay = retry_delay(response.headers.get("Retry-After"), attempt)
        if status == 429 or 500 <= status <= 599:
            with self.rate_lock:
                self.rate_until = max(self.rate_until, time.monotonic() + delay)
        message = "Токен недействителен или истёк" if status == 401 else f"API вернул HTTP {status}"
        raise APIError(message, status, delay)

    def get(self, endpoint, params=None, settings=None, checkpoint=lambda: None):
        if endpoint not in ("/resources", "/resources/download", ""):
            raise ValueError("Разрешены только методы чтения Диска")
        settings = settings or {}
        tries = 1 if settings.get("_single_attempt") else attempt_count(settings)
        timeout = settings.get("timeout", 30)
        _, epoch = self.credentials.snapshot()

        def check():
            self.credentials.check(epoch)
            checkpoint()
            if budget:
                budget.check()

        budget = None
        delay = 0
        control = getattr(checkpoint, "__self__", None)
        network = getattr(control, "network", None)
        owner = settings.get("_deadline")
        attempt = 0
        while attempt < tries:
            response = session = None
            if network and not owner:
                network.wait(checkpoint)
            try:
                with nullcontext(owner) if owner else Deadline(timeout, checkpoint, getattr(control, "active_clock", time.monotonic), lambda: getattr(control, "stopped", False) or not self.credentials.connected) as budget:
                    check.deadline = budget
                    if delay:
                        self.wait(delay, check)
                    response, session = self._open(BASE + endpoint, params, True, timeout, check, epoch)
                    if response.status_code == 401:
                        self.credentials.clear()
                        raise Suspended("Токен недействителен или истёк. Подключите Диск заново.")
                    if response.status_code != 200:
                        self.fail_response(response, attempt)
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        check()
                        data.extend(chunk)
                        if len(data) > 32 * 1024 * 1024:
                            raise APIError("Слишком большой ответ метаданных API")
                    check()
                    import json
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise APIError("Некорректный ответ API")
                    return result
            except (requests.RequestException, AttributeError, OSError):
                error = NetworkError(offline=True) if budget and budget.network_aborted.is_set() else NetworkError(f"Превышен лимит попытки: {timeout} с" if budget and budget.expired.is_set() else "Сетевая ошибка или таймаут")
            except (ValueError, UnicodeError):
                error = NetworkError(offline=True) if budget and budget.network_aborted.is_set() else APIError("Некорректный JSON API")
            except APIError as exc:
                error = exc
            finally:
                if response is not None:
                    self.close(response, session)
            budget = None
            if network and not owner:
                def connected_check():
                    self.credentials.check(epoch)
                    checkpoint()
                if network.recover(error, connected_check):
                    delay = 0
                    continue
            if attempt == tries - 1 or (error.status is not None and error.status != 429 and error.status < 500):
                raise error
            delay = error.retry_after if error.retry_after is not None else retry_delay(None, attempt)
            attempt += 1

    def metadata(self, path, settings=None, checkpoint=lambda: None, offset=0, limit=200, sort="name", preview_size=None):
        settings = settings or {}
        path = remote_path(path)
        result = self.get("/resources", {"path": path, "limit": limit, "offset": offset, "sort": sort,
                          "preview_size": preview_size or settings.get("preview_size", "XXXL"), "preview_crop": "false"}, settings, checkpoint)
        if remote_path(result.get("path", "")) != path or result.get("type") not in ("file", "dir"):
            raise APIError("API вернул неожиданный путь или тип ресурса")
        return result

    def media(self, path, kind, settings, checkpoint=lambda: None):
        if kind == "original":
            result = self.get("/resources/download", {"path": remote_path(path)}, settings, checkpoint)
            if result.get("method", "GET") != "GET" or result.get("templated", False):
                raise APIError("Неожиданный тип ссылки API")
            url = result.get("href", "")
        else:
            meta = settings.get("_preview_meta")
            url = (meta if meta is not None else self.metadata(path, settings, checkpoint)).get("preview")
            if not url:
                raise APIError("Превью отсутствует", 404)
        return self.open_media(url, kind == "preview", settings, checkpoint)

    def open_media(self, url, preview, settings, checkpoint=lambda: None):
        _, epoch = self.credentials.snapshot()

        def check():
            self.credentials.check(epoch)
            checkpoint()
            if settings.get("_deadline"):
                settings["_deadline"].check()

        check.deadline = settings.get("_deadline")

        for _ in range(6):
            if not trusted_media(url):
                raise APIError("API вернул недопустимый адрес загрузки")
            host = urlsplit(url).hostname
            auth = preview and (host == "cloud-api.yandex.net" or host in ("downloader.disk.yandex.ru", "downloader.disk.yandex.net", "downloader.disk.yandex.com"))
            offset = settings.get("_range_offset", 0)
            extra = {"Range": f"bytes={offset}-"} if offset else {}
            if offset and settings.get("_range_etag"):
                extra["If-Range"] = settings["_range_etag"]
            if extra:
                response, session = self._open(url, None, auth, settings.get("timeout", 30), check, epoch, extra_headers=extra)
            else:
                response, session = self._open(url, None, auth, settings.get("timeout", 30), check, epoch)
            if response.status_code in (301, 302, 303, 307, 308):
                new_url = urljoin(url, response.headers.get("Location", ""))
                self.close(response, session)
                if new_url == url:
                    raise APIError("Некорректное перенаправление загрузки")
                url = new_url
                continue
            if response.status_code not in ((200, 206) if offset else (200,)):
                try:
                    self.fail_response(response)
                finally:
                    self.close(response, session)
            return response, session
        raise APIError("Слишком много перенаправлений загрузки")
