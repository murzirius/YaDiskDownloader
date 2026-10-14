import json
import re
import threading
import time
from urllib.parse import urlsplit

import requests

from . import __version__
from .api import APIError, Deadline, DeadlineAdapter


DEFAULT_REPOSITORY = "murzirius/YaDiskDownloader"


def repository(value):
    if not isinstance(value, str):
        raise ValueError("Укажите репозиторий GitHub")
    value = value.strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError("Нужна ссылка https://github.com/владелец/репозиторий")
        value = parsed.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Нужен адрес GitHub в формате владелец/репозиторий")
    return value


def version(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


class Updates:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.busy = False
        self.last_check = 0
        self.result = {"current": __version__, "available": False, "message": "Проверка ещё не выполнялась"}

    def settings(self):
        with self.store.db() as db:
            row = db.execute("SELECT value FROM preferences WHERE key='updates'").fetchone()
        return json.loads(row[0]) if row else {"repository": DEFAULT_REPOSITORY, "automatic": True}

    def save(self, data):
        if not isinstance(data.get("automatic", True), bool):
            raise ValueError("Некорректная настройка обновлений")
        value = {"repository": repository(data.get("repository", DEFAULT_REPOSITORY)), "automatic": data.get("automatic", True)}
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('updates',?)", (json.dumps(value),))
        return value

    def start(self, force=False):
        with self.lock:
            if self.busy or not force and (not self.settings()["automatic"] or self.last_check and time.monotonic() - self.last_check < 3600):
                return
            self.busy = True
        threading.Thread(target=self.check, name="disk-updates", daemon=True).start()

    def check(self):
        repo = self.settings()["repository"]
        result = {"current": __version__, "repository": repo, "available": False}
        try:
            with Deadline(8) as deadline, requests.Session() as session:
                session.trust_env = False
                session.mount("https://", DeadlineAdapter(deadline))
                response = session.get("https://api.github.com/repos/" + repo + "/releases/latest", headers={"Accept": "application/vnd.github+json", "User-Agent": "YaDiskDownloader/" + __version__}, timeout=(3, 5), stream=True, allow_redirects=False)
                deadline.attach(response=response)
                with response:
                    if response.status_code == 404:
                        result["message"] = "Публичный релиз пока не найден"
                    elif response.status_code in (403, 429):
                        result["message"] = "GitHub временно ограничил проверку. Попробуйте позже."
                    elif response.status_code != 200:
                        result["message"] = "GitHub недоступен для проверки"
                    else:
                        payload = bytearray()
                        started = time.monotonic()
                        for chunk in response.iter_content(65536):
                            deadline.check()
                            payload.extend(chunk)
                            if len(payload) > 1024 * 1024 or time.monotonic() - started > 8:
                                raise ValueError("Invalid response")
                        release = json.loads(payload)
                        tag = release.get("tag_name", "")
                        target = urlsplit(release.get("html_url", ""))
                        if target.scheme != "https" or target.hostname != "github.com" or not target.path.casefold().startswith(("/" + repo + "/releases/tag/").casefold()) or target.port not in (None, 443) or target.username or target.password or target.query or target.fragment or len(tag) > 100:
                            raise ValueError("Invalid release")
                        latest = version(tag)
                        result.update(latest=tag, url=target.geturl(), available=bool(latest and latest > version(__version__)))
                        result["message"] = "Доступна новая версия " + tag if result["available"] else "Новая версия не требуется" if latest else "Найден релиз " + tag + "; сравните версии вручную"
        except (APIError, requests.RequestException, ValueError, TypeError, AttributeError):
            result["message"] = "Не удалось проверить обновления. Проверьте соединение и попробуйте позже."
        finally:
            with self.lock:
                self.result = result
                self.last_check = time.monotonic()
                self.busy = False

    def snapshot(self):
        with self.lock:
            return dict(self.result, busy=self.busy, settings=self.settings())
