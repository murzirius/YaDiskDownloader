"""Browser command/profile contracts exercised through exact shipped handlers."""
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import test_timeout


@pytest.mark.parametrize("finding", ["AUD-02", "AUD-03", "AUD-16", "R-01"])
def test_browser_remediation_contracts(finding):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser contract tests")
    script = Path(__file__).with_name("remediation_ui.cjs")
    result = subprocess.run(
        [node, "--test", "--test-name-pattern=" + finding, str(script)],
        capture_output=True, text=True, encoding="utf-8", timeout=test_timeout(30),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_profile_late_settings_survive_save_reload_and_job_creation(tmp_path):
    from conftest import wait_job
    from test_api_security import web_session

    app, client, base, headers = web_session(tmp_path)
    engine = app.extensions["engine"]
    engine.api.file("disk:/photo.jpg", b"synthetic", preview=True)
    engine.api.tree["disk:/photo.jpg"]["created"] = "2020-06-01T12:00:00Z"
    settings = {
        "destination": str(tmp_path / "results"), "whole_disk": True,
        "mode": "both", "auto_start": False, "reports": ["json"],
        "workers": 5, "attempts": 3, "timeout": 41, "total_timeout": 91,
        "create_zip": False, "resume_parts": False, "auto_pause": False,
        "max_disk_percent": 91, "speed_limit_mib": 3.5,
        "verification_mode": "full", "deduplicate": True,
        "date_from": "2020-01-01", "date_to": "2020-12-31",
        "date_field": "created", "include_unknown_dates": False,
        "priority_paths": ["disk:/"], "prevent_sleep": True,
        "run_at": "2030-01-01T12:34:00.000Z",
        "quiet_hours": {"enabled": True, "start": 21, "end": 7,
                        "speed_limit_mib": 1.5, "mute": False},
    }
    response = client.post("/api/profiles", base_url=base, headers=headers,
                           json={"name": "Synthetic full profile", "settings": settings})
    assert response.status_code == 200, response.json
    loaded = client.get("/api/profiles", base_url=base, headers=headers).json["items"][0]["settings"]
    for key, value in settings.items():
        assert loaded[key] == value, key
    response = client.post("/api/jobs", base_url=base, headers=headers, json=loaded)
    assert response.status_code == 200, response.json
    job_id = response.json["id"]
    result = wait_job(engine, job_id)
    assert result["phase"] == "ready"
    for key, value in settings.items():
        assert engine.store.job(job_id)["settings"][key] == value, key
    assert engine.api.media_calls == []
