import threading
import socket
import time
import math
import re
import io
import http.client
import sys
from contextlib import nullcontext, contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util.connection import allowed_gai_family

from .paths import remote_path
from .dns import Resolver


BASE = "https://cloud-api.yandex.net/v1/disk"
SIZES = ("S", "M", "L", "XL", "XXL", "XXXL")
PREVIEW_MAX_BYTES = 64 * 1024 * 1024
DNS_RESOLVER = Resolver()


class Suspended(Exception):
    pass


class PausedRequest(Exception):
    """Retry an interrupted request after a user pause, without spending an attempt."""


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
    def __init__(self, seconds, checkpoint=lambda: None, clock=time.monotonic, cancelled=lambda: False, idle_seconds=None):
        self.seconds = seconds
        self.idle_seconds = idle_seconds
        self.checkpoint = checkpoint
        self.clock = clock
        self.cancelled = cancelled
        self.network = getattr(getattr(checkpoint, "__self__", None), "network", None)
        self.network_aborted = threading.Event()
        self.pause_aborted = threading.Event()
        self.started = clock()
        self.last_data = 0
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
        try:
            self.thread.start()
        except BaseException:
            self.finished.set()
            if self.network:
                self.network.unregister(self)
            raise
        return self

    def __exit__(self, kind, value, traceback):
        self.finished.set()
        if self.network:
            self.network.unregister(self)
        self.abort()
        self.thread.join(0.2)

    def elapsed(self):
        current = self.clock()
        return (self.hold_started if self.hold_started is not None else current) - self.started - self.held

    def remaining(self):
        elapsed = self.elapsed()
        total = max(0, self.seconds - elapsed) if self.seconds else float('inf')
        idle = max(0, self.idle_seconds - (elapsed - self.last_data)) if self.idle_seconds else float('inf')
        return min(total, idle)

    def touch(self):
        self.last_data = self.elapsed()

    def received(self):
        """The body is complete; local verification isn't a network stall."""
        self.idle_seconds = None

    def timeout_message(self):
        if self.idle_seconds and self.elapsed() - self.last_data >= self.idle_seconds:
            return f'Нет новых данных {self.idle_seconds} с'
        return f'Превышен лимит попытки: {self.seconds} с'

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
        if self.pause_aborted.is_set():
            raise PausedRequest()
        if self.network_aborted.is_set() or self.network and self.network.offline.is_set():
            raise NetworkError(offline=True)
        if self.expired.is_set() or self.remaining() <= 0:
            self.expired.set()
            raise NetworkError(self.timeout_message())

    def attach(self, connection=None, response=None, sock=None):
        with self.lock:
            if sock is not None:
                self.sockets.append(sock)
            if connection is not None:
                self.connections.append(connection)
            if response is not None:
                raw = getattr(response, "raw", None)
                fp = getattr(getattr(raw, "_fp", None), "fp", None)
                sock = getattr(getattr(fp, "raw", None), "_sock", None)
                if sock is not None:
                    self.sockets.append(sock)
        self.check()

    def detach_socket(self, sock):
        with self.lock:
            self.sockets = [item for item in self.sockets if item is not sock]

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
            if self.pause_aborted.is_set():
                self.abort()
                continue
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

        def new_socket(connection):
            host, port = connection._dns_host, connection.port
            family = allowed_gai_family()
            try:
                try:
                    addresses = DNS_RESOLVER.resolve(host,port,family,deadline.check)
                except RuntimeError:
                    raise APIError('Приложение на ПК не смогло запустить определение адреса сервера. Закройте лишние программы и повторите загрузку.') from None
                except NetworkError:
                    if deadline.expired.is_set():
                        raise NetworkError('Не удалось определить адрес сервера (DNS): ' + deadline.timeout_message()) from None
                    raise
                error = None
                for af, kind, protocol, _, address in addresses:
                    deadline.check()
                    sock = None
                    try:
                        sock = socket.socket(af,kind,protocol)
                        deadline.attach(sock=sock)
                        for option in connection.socket_options or ():
                            sock.setsockopt(*option)
                        limit = connection.timeout
                        remaining = deadline.remaining()
                        limit = min(limit,remaining) if isinstance(limit,(int,float)) else remaining
                        sock.settimeout(limit if math.isfinite(limit) else None)
                        if connection.source_address:
                            sock.bind(connection.source_address)
                        sock.connect(address)
                        deadline.check()
                        # TLS starts after _new_conn(). It gets the remaining
                        # budget, not the original pre-DNS/connect timeout.
                        limit = min(limit,deadline.remaining())
                        sock.settimeout(limit if math.isfinite(limit) else None)
                        sys.audit('http.client.connect',connection,connection.host,connection.port)
                        return sock
                    except BaseException as exc:
                        if sock is not None:
                            deadline.detach_socket(sock)
                            sock.close()
                        if not isinstance(exc,OSError):
                            raise
                        error = exc
                DNS_RESOLVER.invalidate(host,port,family)
                if error is not None:
                    raise error
                raise OSError('System DNS returned no addresses')
            except socket.gaierror as exc:
                raise NameResolutionError(connection.host,connection,exc) from exc
            except socket.timeout as exc:
                raise ConnectTimeoutError(connection,'Истекло время подключения к серверу') from exc
            except OSError as exc:
                raise NewConnectionError(connection,'Не удалось подключиться к серверу') from exc

        class TimedHTTPConnection(HTTPConnection):
            def _new_conn(self):
                return new_socket(self)

        class TimedHTTPSConnection(HTTPSConnection):
            def _new_conn(self):
                return new_socket(self)

        class ProgressRaw(io.RawIOBase):
            def __init__(self, raw):
                self.raw = raw

            def readable(self):
                return True

            def readinto(self, buffer):
                count = self.raw.readinto(buffer)
                if count:
                    deadline.touch()
                return count

            def fileno(self):
                return self.raw.fileno()

            @property
            def _sock(self):
                return self.raw._sock

            def close(self):
                try:
                    self.raw.close()
                finally:
                    super().close()

        class ProgressResponse(http.client.HTTPResponse):
            def __init__(self, *args, **options):
                super().__init__(*args, **options)
                self.fp = io.BufferedReader(ProgressRaw(self.fp.detach()))

        class TimedHTTPPool(HTTPConnectionPool):
            ConnectionCls = TimedHTTPConnection
            def _new_conn(self):
                conn = super()._new_conn()
                conn.response_class = ProgressResponse
                deadline.attach(connection=conn)
                return conn

        class TimedHTTPSPool(HTTPSConnectionPool):
            ConnectionCls = TimedHTTPSConnection
            def _new_conn(self):
                conn = super()._new_conn()
                conn.response_class = ProgressResponse
                deadline.attach(connection=conn)
                return conn

        self.poolmanager.pool_classes_by_scheme = {"http": TimedHTTPPool, "https": TimedHTTPSPool}


def retry_delay(value, attempt):
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return min(300, max(0, seconds))
        except (ValueError, TypeError):
            try:
                return min(300, max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass
    return min(30, 0.5 * 2 ** attempt)


def validate_resource(meta):
    try:
        if not isinstance(meta, dict) or meta.get("type") not in ("file", "dir"):
            raise ValueError()
        remote_path(meta.get("path"))
        size = meta.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 0 <= size < 2 ** 63):
            raise ValueError()
        for key, length in (("md5", 32), ("sha256", 64)):
            value = meta.get(key)
            if value and (not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{" + str(length) + r"}", value)):
                raise ValueError()
        if "name" in meta and not isinstance(meta["name"], str):
            raise ValueError()
        embedded = meta.get("_embedded")
        if embedded is not None:
            if not isinstance(embedded, dict) or not isinstance(embedded.get("items"), list):
                raise ValueError()
            total = embedded.get("total")
            if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
                raise ValueError()
        return meta
    except ValueError:
        raise APIError("API вернул некорректные метаданные ресурса") from None


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
        self.reason = "not_connected"

    def set(self, token, expected_epoch=None):
        if not isinstance(token, str) or not 10 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError("Ключ доступа не похож на токен Яндекса. Скопируйте весь токен, без пробелов и переноса строки. Нужен токен, а не ClientID и не Client_secret. Подсказка — кнопка «Где взять ключ?».")
        with self.lock:
            if expected_epoch is not None and expected_epoch != self.epoch:
                raise ValueError("Подключение изменилось в другой вкладке. Обновите состояние и при необходимости подключите ключ ещё раз.")
            active = list(self.responses)
            self.responses.clear()
            self.epoch += 1
            self.token = token
            self.reason = ""
        for response in active:
            response.close()

    def clear(self, expected_epoch=None, reason="disconnected"):
        with self.lock:
            if expected_epoch is not None and expected_epoch != self.epoch:
                return False
            self.token = None
            self.reason = reason
            self.epoch += 1
            active = list(self.responses)
            self.responses.clear()
        for response in active:
            response.close()
        return True

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

    def status(self):
        with self.lock:
            return {"connected": bool(self.token), "auth_revision": self.epoch, "auth_reason": self.reason}


class DiskAPI:
    def __init__(self, credentials):
        self.credentials = credentials
        self.rate_lock = threading.Lock()
        self.rate_until = 0

    def verify_token(self, token):
        """Validate a candidate without revoking the current working connection."""
        candidate = Credentials()
        candidate.set(token)
        try:
            DiskAPI(candidate).metadata("disk:/", {"attempts": 1, "timeout": 15, "total_timeout": 20}, limit=1)
        finally:
            candidate.clear()

    def wait(self, delay, checkpoint):
        control = getattr(checkpoint, 'control', None) or getattr(checkpoint, '__self__', None)
        if isinstance(control, Deadline):control = getattr(control.checkpoint, '__self__', None)
        context = control.expected_wait() if delay and hasattr(control, 'expected_wait') else nullcontext()
        with context:
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
        with deadline.hold() if (deadline := getattr(checkpoint, "deadline", None)) else nullcontext():
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
                    raise NetworkError(deadline.timeout_message()) from None
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
        messages = {
            401: "Яндекс.Диск отклонил ключ доступа. Получите новый ключ с правом чтения Диска и подключитесь снова.",
            403: "Яндекс.Диск запретил доступ (HTTP 403). Проверьте право чтения у ключа и доступ к этому файлу. Если другие файлы скачиваются, повторите этот файл позже.",
            404: "Яндекс.Диск не нашёл файл или его превью (HTTP 404). Файл мог быть перемещён или удалён; превью бывает недоступно.",
            429: "Яндекс.Диск временно ограничил число запросов (HTTP 429). Программа выдержит паузу; при частых ограничениях уменьшите число одновременных загрузок.",
        }
        message = messages.get(status, f"Сервер Яндекс.Диска вернул ошибку HTTP {status}. Повторите позже; при массовых ошибках проверьте доступность Диска.")
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
        check.control = control
        network = getattr(control, "network", None)
        owner = settings.get("_deadline")
        attempt = 0
        while attempt < tries:
            response = session = None
            if network and not owner:
                network.wait(checkpoint)
            try:
                if delay:
                    self.wait(delay, check)
                with nullcontext(owner) if owner else Deadline(settings.get('total_timeout', timeout), checkpoint, getattr(control, "active_clock", time.monotonic), lambda: getattr(control, "stopped", False) or not self.credentials.connected, idle_seconds=timeout if 'total_timeout' in settings else None) as budget:
                    check.deadline = budget
                    response, session = self._open(BASE + endpoint, params, True, timeout, check, epoch)
                    if response.status_code == 401:
                        self.credentials.clear(expected_epoch=epoch, reason="expired")
                        raise Suspended("Яндекс.Диск отклонил ключ доступа. Подключите Диск заново с действующим ключом и нажмите «Продолжить» у задания. Скачанные файлы сохранены.")
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
            except PausedRequest:
                if owner:
                    raise
                delay = 0
                continue
            except (requests.RequestException, AttributeError, OSError):
                error = NetworkError(offline=True) if budget and budget.network_aborted.is_set() else NetworkError(budget.timeout_message() if budget and budget.expired.is_set() else "Сетевая ошибка или таймаут")
            except (ValueError, UnicodeError):
                error = NetworkError(offline=True) if budget and budget.network_aborted.is_set() else APIError("Некорректный JSON API")
            except APIError as exc:
                error = exc
            finally:
                if response is not None:
                    self.close(response, session)
            if getattr(budget, 'pause_aborted', None) and budget.pause_aborted.is_set():
                if owner:
                    raise PausedRequest()
                checkpoint()
                delay = 0
                continue
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
        validate_resource(result)
        if result["path"] != path:
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
