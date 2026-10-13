import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from conftest import FakeAPI, Response
from disk_downloader.api import APIError, BASE, Credentials, DiskAPI, Suspended, retry_delay, trusted_media
from disk_downloader.paths import PathError, component, normalize_selection, remote_path, safe_join
from disk_downloader.web import create_app


@pytest.mark.parametrize("value", ["disk:/../evil", "disk:/a/../../evil", "disk:/a//b", "C:/evil", "disk:/a/./b", "disk:/a\x00b"])
def test_reject_remote_traversal(value):
    with pytest.raises(PathError):
        remote_path(value)


@pytest.mark.parametrize("value", ["../outside", "a/../../outside", "/absolute", "C:/outside", "a\\outside", "a//b"])
def test_reject_local_traversal(tmp_path, value):
    with pytest.raises(PathError):
        safe_join(tmp_path, value)


def test_symlink_escape(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Права Windows не позволяют создать symlink")
    with pytest.raises(PathError):
        safe_join(root, "link/stolen")


def test_windows_names_deterministic():
    for name in ("CON", "con.txt", "LPT1", "COM¹.txt", "x:y", "x?y", "trailing. ", "a" * 300, "💾" * 100):
        result = component(name, "disk:/" + name)
        assert result == component(name, "disk:/" + name)
        assert len(result.encode("utf-16-le")) <= 510
        assert not any(c in result for c in ':*?"<>|\\/')
    assert component("photo.JPG", "disk:/photo.JPG") == "photo.JPG"
    assert normalize_selection(["disk:/a/b", "disk:/a", "disk:/abc", "disk:/a"], False) == ["disk:/a", "disk:/abc"]


@pytest.mark.parametrize("url", ["http://downloader.disk.yandex.ru/a", "https://evil.test/a", "https://disk.yandex.ru.evil.test/a", "https://downloader.disk.yandex.ru:123/a", "https://user:secret@downloader.disk.yandex.ru/a", "https://127.0.0.1/a", "file:///C:/file"])
def test_reject_media_addresses(url):
    assert not trusted_media(url)


def test_api_429_503_retry_after_without_logging_urls(monkeypatch):
    credentials = Credentials()
    credentials.set("dummy-safe-test-token")
    api = DiskAPI(credentials)
    responses = [Response(b"", 429, {"Retry-After": "2"}), Response(b"", 503, {"Retry-After": "0"}), Response(b'{"ok":true}')]
    delays = []

    def opened(url, params, auth, timeout, check, epoch):
        assert url == BASE + "/resources"
        assert auth
        return responses.pop(0), None

    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    monkeypatch.setattr(api, "wait", lambda delay, check: delays.append(delay))
    assert api.get("/resources", settings={"retries": 2}) == {"ok": True}
    assert delays == [2]
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert 8 <= retry_delay(future, 0) <= 11


def test_expired_download_link_refetched_and_redirect_drops_auth(monkeypatch):
    credentials = Credentials()
    credentials.set("dummy-safe-test-token")
    api = DiskAPI(credentials)
    links = []
    opens = []

    def get(endpoint, params, settings, checkpoint):
        links.append(endpoint)
        return {"href": f"https://downloader.disk.yandex.ru/download/{len(links)}", "method": "GET"}

    def opened(url, params, auth, timeout, check, epoch):
        opens.append((url, auth))
        if url.endswith("/1"):
            return Response(b"", 403), None
        return Response(b"data"), None

    monkeypatch.setattr(api, "get", get)
    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    with pytest.raises(APIError):
        api.media("disk:/a", "original", {"timeout": 5})
    response, session = api.media("disk:/a", "original", {"timeout": 5})
    assert response.data == b"data" and len(links) == 2
    assert all(not auth for url, auth in opens)
    opens.clear()

    def redirected(url, params, auth, timeout, check, epoch):
        opens.append((url, auth))
        if "downloader" in url:
            return Response(b"", 302, {"Location": "https://node.storage.yandex.net/image"}), None
        return Response(b"image"), None

    monkeypatch.setattr(api, "_open", redirected)
    api.open_media("https://downloader.disk.yandex.ru/preview/1", True, {"timeout": 5})
    assert opens[0][1] and not opens[1][1]


def test_external_redirect_never_requested(monkeypatch):
    credentials = Credentials()
    credentials.set("dummy-safe-test-token")
    api = DiskAPI(credentials)
    calls = []

    def opened(url, *args):
        calls.append(url)
        return Response(b"", 302, {"Location": "https://evil.test/steal"}), None

    monkeypatch.setattr(api, "_open", opened)
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    with pytest.raises(APIError):
        api.open_media("https://downloader.disk.yandex.ru/preview", True, {"timeout": 5})
    assert len(calls) == 1


def test_disconnect_revokes_generation_and_closes_responses():
    credentials = Credentials()
    credentials.set("dummy-safe-test-token")
    _, epoch = credentials.snapshot()
    response = Response(b"data")
    credentials.responses.add(response)
    credentials.clear()
    assert credentials.token is None and response.closed
    with pytest.raises(Suspended):
        credentials.check(epoch)
    credentials.set("new-dummy-safe-token")
    with pytest.raises(Suspended):
        credentials.check(epoch)


def test_expired_oauth_stops_and_clears_token(monkeypatch):
    credentials = Credentials()
    credentials.set("dummy-safe-test-token")
    api = DiskAPI(credentials)
    response = Response(b"", 401)
    monkeypatch.setattr(api, "_open", lambda *args: (response, None))
    monkeypatch.setattr(api, "close", lambda response, session: response.close())
    with pytest.raises(Suspended):
        api.get("/resources")
    assert not credentials.connected and response.closed


def web_session(tmp_path):
    app = create_app(tmp_path / "state", api=FakeAPI(), port=8765)
    client = app.test_client()
    base = "http://127.0.0.1:8765"
    page = client.get("/", base_url=base)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.get_data(as_text=True)).group(1)
    headers = {"X-CSRF-Token": csrf, "Origin": base}
    return app, client, base, headers


def test_host_origin_csrf_and_download_session(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    assert app.config["SESSION_COOKIE_NAME"] == "disk_downloader_8765"
    assert client.get("/api/status", base_url=base, headers=headers).status_code == 200
    assert client.get("/api/status", base_url=base).status_code == 403
    assert client.get("/", base_url="http://evil.test:8765").status_code == 403
    assert client.post("/api/disconnect", base_url=base, headers=headers | {"Origin": "https://evil.test"}, json={}).status_code == 403
    assert client.post("/api/disconnect", base_url=base, headers={"X-CSRF-Token": headers["X-CSRF-Token"]}, json={}).status_code == 403
    assert client.get("/", base_url=base, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/download/nonexistent/zip", base_url=base).status_code == 403
    assert "frame-ancestors 'none'" in client.get("/", base_url=base).headers["Content-Security-Policy"]


def test_web_settings_validation_and_disconnect(tmp_path):
    app, client, base, headers = web_session(tmp_path)
    for data in ({"destination": "relative", "whole_disk": True}, {"destination": str(tmp_path), "whole_disk": True, "mode": "bad"}, {"destination": str(tmp_path), "whole_disk": True, "reports": ["md"]}, {"destination": str(tmp_path), "whole_disk": True, "workers": 99}):
        assert client.post("/api/jobs", base_url=base, headers=headers, json=data).status_code == 400
    assert client.post("/api/disconnect", base_url=base, headers=headers, json={}).status_code == 200
    assert not app.extensions["disk_api"].credentials.connected
    assert not (tmp_path / "state" / "token").exists()


def test_streamed_zip_download_with_local_session(tmp_path):
    from conftest import wait_job
    app, client, base, headers = web_session(tmp_path)
    result = client.post("/api/jobs", base_url=base, headers=headers, json={"destination": str(tmp_path / "results"), "whole_disk": True, "reports": ["csv"]})
    assert result.status_code == 200
    job_id = result.get_json()["id"]
    engine = app.extensions["engine"]
    assert wait_job(engine, job_id)["phase"] == "ready"
    assert client.post(f"/api/jobs/{job_id}/action", base_url=base, headers=headers, json={"action": "start"}).status_code == 200
    assert wait_job(engine, job_id)["phase"] == "completed"
    response = client.get(f"/download/{job_id}/zip", base_url=base)
    assert response.status_code == 200 and response.is_streamed
    assert response.content_type == "application/zip"
    assert response.get_data().startswith(b"PK")
    response.close()


def test_open_zip_folder_does_not_send_a_browser_download(tmp_path, monkeypatch):
    from conftest import wait_job
    import os
    app, client, base, headers = web_session(tmp_path)
    called = []
    monkeypatch.setattr(os, "startfile", lambda path, verb: called.append((path, verb)), raising=False)
    result = client.post("/api/jobs", base_url=base, headers=headers, json={"destination": str(tmp_path / "chosen"), "whole_disk": True})
    job_id = result.get_json()["id"]
    engine = app.extensions["engine"]
    wait_job(engine, job_id)
    engine.action(job_id, "start")
    job = wait_job(engine, job_id)
    response = client.post(f"/api/jobs/{job_id}/open-output", base_url=base, headers=headers, json={})
    assert response.status_code == 200 and response.is_json
    assert "Content-Disposition" not in response.headers
    assert response.get_json()["path"] == job["root"]
    if os.name == "nt":
        assert len(called) == 1 and called[0][1] == "explore"
