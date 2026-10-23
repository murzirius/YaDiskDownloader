"""Updater fault boundaries; every executable and process is synthetic."""
import errno
import hashlib
import io
import zipfile
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from disk_downloader import updater
from disk_downloader.paths import native


OLD = b"MZ" + b"known-good-old-program" * 80
NEW = b"MZ" + b"new-program-under-test" * 80


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def update_files(tmp_path):
    stage = tmp_path / "updates" / ("a" * 32)
    stage.mkdir(parents=True)
    target = tmp_path / "YaDiskDownloader.exe"
    candidate = stage / "new.exe"
    target.write_bytes(OLD)
    candidate.write_bytes(NEW)
    state = tmp_path / "state"
    backups = state / "backups"
    backups.mkdir(parents=True)
    backup_name = "before-1.5.0.sqlite3"
    for filename in (state / "jobs.sqlite3", backups / backup_name):
        with sqlite3.connect(filename) as db:
            db.execute("CREATE TABLE marker(value TEXT)")
            db.execute("INSERT INTO marker VALUES('before update')")
    config = dict(target=str(target), candidate=str(candidate), sha256=sha(NEW),
                  old_sha256=sha(OLD), state_dir=str(state), backup=backup_name,
                  pid=987654321, port=18766, version="9.0.0", startup_timeout=5)
    filename = stage / "install.json"
    filename.write_text(json.dumps(config), encoding="utf-8")
    return SimpleNamespace(stage=stage, target=target, candidate=candidate,
                           state=state, config=config, filename=filename)


@pytest.fixture
def launches(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "wait_process", lambda *args: True)
    monkeypatch.setattr(updater.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(updater.subprocess, "CREATE_NO_WINDOW", 0, raising=False)

    def launch(args, **kwargs):
        calls.append(list(args))
        return SimpleNamespace(pid=123456789, poll=lambda: 1, wait=lambda **kw: 1)

    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="Windows EXE installation preflight")
def test_renamed_executable_rejected_before_backup_or_shutdown(system, tmp_path, monkeypatch):
    engine, _, _ = system
    target = tmp_path / "RenamedDownloader.exe"
    target.write_bytes(OLD)
    stage = Path(engine.store.filename).parent / "updates" / ("b" * 32)
    stage.mkdir(parents=True)
    candidate = stage / "new.exe"
    candidate.write_bytes(NEW)
    backup = Mock(return_value="unused.sqlite3")
    install = updater.Installer(engine, SimpleNamespace(settings=lambda: {"repository": "example/project"}),
                                SimpleNamespace(create=backup), 18766)
    install.prepared = dict(stage=str(stage), candidate=str(candidate), sha256=sha(NEW),
                            version="9.0.0", repository="example/project")
    install.result = {"state": "ready", "bytes": 0}
    launch, timer, quit_app = Mock(), Mock(), Mock()
    monkeypatch.setattr(updater, "frozen", lambda: True)
    monkeypatch.setattr(updater.sys, "executable", str(target))
    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    monkeypatch.setattr(updater.threading, "Timer", timer)

    with pytest.raises(ValueError, match="YaDiskDownloader.exe"):
        install.install(quit_app)

    assert not engine.shutting_down
    assert target.read_bytes() == OLD
    assert install.snapshot()["state"] == "ready"
    backup.assert_not_called()
    launch.assert_not_called()
    timer.assert_not_called()
    quit_app.assert_not_called()


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
@pytest.mark.parametrize("destination", ["previous", "replacement"])
def test_post_exit_copy_failure_relaunches_unchanged_old_program(update_files, launches, monkeypatch, code, destination):
    files = update_files
    copy = shutil.copyfile

    def fail(source, target, *args, **kwargs):
        selected = Path(target).name == "previous.exe" if destination == "previous" else Path(target).name.startswith(".update-")
        if selected:
            Path(target).write_bytes(b"incomplete synthetic copy")
            raise OSError(code, "synthetic copy failure")
        return copy(source, target, *args, **kwargs)

    monkeypatch.setattr(updater.shutil, "copyfile", fail)
    updater.apply_update(files.filename)

    assert files.target.read_bytes() == OLD
    assert len(launches) == 1 and Path(launches[0][0]) == files.target
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and result["restarted"] is True
    assert result["stage"] in ("backup_executable", "prepare_replacement")


def test_candidate_changed_after_preflight_relaunches_only_verified_old_program(update_files, launches):
    files = update_files
    files.candidate.write_bytes(b"changed synthetic candidate")
    updater.apply_update(files.filename)
    assert files.target.read_bytes() == OLD
    assert len(launches) == 1 and Path(launches[0][0]) == files.target
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and result["stage"] == "verify_candidate"


def test_invalid_helper_paths_never_launch_a_configured_program(update_files, launches):
    files = update_files
    files.config["candidate"] = str(files.target)
    files.filename.write_text(json.dumps(files.config), encoding="utf-8")
    with pytest.raises(ValueError):
        updater.apply_update(files.filename)
    assert not launches and files.target.read_bytes() == OLD


def test_failed_new_start_restores_old_program_and_database(update_files, launches, monkeypatch):
    files = update_files
    original_launch = updater.subprocess.Popen

    def launched(args, **kwargs):
        if not launches:
            assert files.target.read_bytes() == NEW
            with sqlite3.connect(files.state / "jobs.sqlite3") as db:
                db.execute("UPDATE marker SET value='new generation'")
        return original_launch(args, **kwargs)

    monkeypatch.setattr(updater.subprocess, "Popen", launched)
    updater.apply_update(files.filename)

    assert files.target.read_bytes() == OLD
    assert (files.stage / "previous.exe").read_bytes() == OLD
    assert len(launches) == 2 and all(Path(call[0]) == files.target for call in launches)
    with sqlite3.connect(files.state / "jobs.sqlite3") as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "before update"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and result["restarted"] is True


def test_failed_rollback_copy_keeps_verified_backup_and_can_relaunch_it(update_files, launches, monkeypatch):
    files = update_files
    copy = shutil.copyfile

    def fail_old_copy(source, target, *args, **kwargs):
        if Path(source).name == "previous.exe":
            raise PermissionError(errno.EACCES, "synthetic rollback access denied")
        return copy(source, target, *args, **kwargs)

    monkeypatch.setattr(updater.shutil, "copyfile", fail_old_copy)
    updater.apply_update(files.filename)

    backup = files.stage / "previous.exe"
    assert backup.read_bytes() == OLD
    assert files.target.read_bytes() == NEW
    assert len(launches) == 2 and Path(launches[-1][0]) == backup
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and result["restarted"] is True
    assert result["restored"] is False


def test_unwritable_failure_report_does_not_prevent_unchanged_relaunch(update_files, launches, monkeypatch):
    files = update_files
    copy, write_text = shutil.copyfile, Path.write_text

    def no_space(source, target, *args, **kwargs):
        if Path(target).name == "previous.exe":
            raise OSError(errno.ENOSPC, "synthetic full device")
        return copy(source, target, *args, **kwargs)

    def report_full(path, *args, **kwargs):
        if path.name == "result.json":
            raise OSError(errno.ENOSPC, "synthetic report failure")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(updater.shutil, "copyfile", no_space)
    monkeypatch.setattr(Path, "write_text", report_full)
    updater.apply_update(files.filename)
    assert files.target.read_bytes() == OLD
    assert len(launches) == 1 and Path(launches[0][0]) == files.target


def test_failed_database_rollback_never_starts_old_program_with_new_history(update_files, launches, monkeypatch):
    files = update_files
    connect = sqlite3.connect

    def no_database(filename, *args, **kwargs):
        if str(filename) == str(files.state / "jobs.sqlite3"):
            raise PermissionError(errno.EACCES, "synthetic history access denied")
        return connect(filename, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", no_database)
    updater.apply_update(files.filename)
    assert len(launches) == 1  # Only the failed new generation was started.
    assert files.target.read_bytes() == OLD
    assert (files.stage / "previous.exe").read_bytes() == OLD
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and result["restarted"] is False
    assert "историю" in result["message"]


def test_unstoppable_new_process_blocks_database_rollback_and_second_start(update_files, launches, monkeypatch):
    files = update_files
    ticks = iter(range(0, 100, 10))
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(ticks))

    def launch(args, **kwargs):
        launches.append(list(args))
        return SimpleNamespace(pid=123456789, poll=lambda: None)

    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    monkeypatch.setattr(updater.subprocess, "run", Mock(side_effect=PermissionError(errno.EACCES, "synthetic child cannot stop")))
    rollback = Mock(side_effect=AssertionError("Cannot restore a live process's history"))
    monkeypatch.setattr(updater, "_restore_update_database", rollback)
    updater.apply_update(files.filename)

    assert len(launches) == 1
    assert files.target.read_bytes() == NEW
    assert (files.stage / "previous.exe").read_bytes() == OLD
    rollback.assert_not_called()
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["restarted"] is False and "ещё работает" in result["message"]


def test_confirmed_new_start_preserves_new_generation_history(update_files, launches, monkeypatch):
    files = update_files

    def launch(args, **kwargs):
        launches.append(list(args))
        (files.stage / "ready.json").write_text(json.dumps({"version": "9.0.0"}), encoding="utf-8")
        with sqlite3.connect(files.state / "jobs.sqlite3") as db:
            db.execute("UPDATE marker SET value='new generation'")
        return SimpleNamespace(pid=123456789, poll=lambda: None)

    class LocalSession:
        trust_env = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    monkeypatch.setattr(updater.requests, "Session", LocalSession)
    updater.apply_update(files.filename)
    assert len(launches) == 1 and files.target.read_bytes() == NEW
    assert (files.stage / "previous.exe").read_bytes() == OLD
    assert json.loads((files.stage / "result.json").read_text(encoding="utf-8"))["ok"] is True
    with sqlite3.connect(files.state / "jobs.sqlite3") as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "new generation"


def test_preexisting_replacement_is_preserved_and_old_program_restarted(update_files, launches):
    files = update_files
    foreign = files.target.parent / (".update-" + files.stage.name + ".exe")
    foreign.write_bytes(b"foreign canary")
    updater.apply_update(files.filename)
    assert foreign.read_bytes() == b"foreign canary"
    assert files.target.read_bytes() == OLD
    assert len(launches) == 1 and Path(launches[0][0]) == files.target


@pytest.mark.parametrize("old_exited", [True, False])
def test_initial_old_digest_read_failure_checks_exit_before_best_effort_relaunch(update_files, launches, monkeypatch, old_exited):
    files = update_files
    digest = updater.digest_file
    reads = []

    def fail_once(filename, *args):
        if Path(filename) == files.target:
            reads.append(filename)
            if len(reads) == 1:
                raise PermissionError(errno.EACCES, "synthetic transient executable read failure")
        return digest(filename, *args)

    wait = Mock(return_value=old_exited)
    monkeypatch.setattr(updater, "digest_file", fail_once)
    monkeypatch.setattr(updater, "wait_process", wait)
    updater.apply_update(files.filename)
    wait.assert_called_once_with(files.config["pid"], 60)
    assert files.target.read_bytes() == OLD
    assert len(launches) == int(old_exited)
    if old_exited:
        assert Path(launches[0][0]) == files.target
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["stage"] == "verify_original" and result["restarted"] is old_exited


def test_initial_old_digest_persistent_failure_never_launches_unverified_program(update_files, launches, monkeypatch):
    files = update_files
    digest = updater.digest_file

    def unreadable(filename, *args):
        if Path(filename) == files.target:
            raise PermissionError(errno.EACCES, "synthetic persistent executable read denial")
        return digest(filename, *args)

    wait = Mock(return_value=True)
    monkeypatch.setattr(updater, "digest_file", unreadable)
    monkeypatch.setattr(updater, "wait_process", wait)
    updater.apply_update(files.filename)
    wait.assert_called_once_with(files.config["pid"], 60)
    assert not launches and files.target.read_bytes() == OLD
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["restarted"] is False and result["ok"] is False


def test_locked_rollback_source_is_bounded_without_starting_old_against_new_history(update_files, launches, monkeypatch):
    from disk_downloader import backups
    files = update_files
    entered, finished = threading.Event(), threading.Event()
    readonly, bounded = backups.readonly, backups.backup_connection
    used_budget, errors = [], []
    original_launch = updater.subprocess.Popen

    def launch(args, **kwargs):
        if not launches:
            with sqlite3.connect(files.state / "jobs.sqlite3") as db:
                db.execute("UPDATE marker SET value='new generation'")
        return original_launch(args, **kwargs)

    def opened(filename):
        source = readonly(filename)
        entered.set()
        return source

    def short_budget(source, target, *args, **kwargs):
        used_budget.append(True)
        return bounded(source, target, timeout=.15)

    def apply():
        try:
            updater.apply_update(files.filename)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    monkeypatch.setattr(backups, "readonly", opened)
    monkeypatch.setattr(backups, "backup_connection", short_budget)
    source_lock = sqlite3.connect(files.state / "backups" / files.config["backup"])
    source_lock.execute("BEGIN EXCLUSIVE")
    worker = threading.Thread(target=apply, daemon=True)
    worker.start()
    try:
        assert entered.wait(3), "Rollback did not reach the locked backup"
        returned_while_locked = finished.wait(3)
    finally:
        source_lock.rollback()
        source_lock.close()
        worker.join(5)

    assert not worker.is_alive() and not errors
    assert returned_while_locked, "Rollback ignored the bounded backup contract"
    assert used_budget == [True]
    assert files.target.read_bytes() == OLD
    assert (files.stage / "previous.exe").read_bytes() == OLD
    assert len(launches) == 1  # The old EXE must not use unrolled-back history.
    with sqlite3.connect(files.state / "jobs.sqlite3") as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "new generation"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    result = json.loads((files.stage / "result.json").read_text(encoding="utf-8"))
    assert result["restarted"] is False and "историю" in result["message"]


@pytest.fixture
def fake_update(monkeypatch):
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w') as archive:
        archive.writestr('YaDiskDownloader/YaDiskDownloader.exe',b'MZ'+b'x'*2048)
    data=payload.getvalue()
    class Response:
        status_code=200;headers={};raw=None
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def close(self):pass
        def iter_content(self,size):yield data
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def mount(self,*args):pass
        def get(self,*args,**kwargs):return Response()
    monkeypatch.setattr(updater.requests,'Session',Session)
    asset={'name':'YaDiskDownloader-v9.0.0-Windows-x64.zip','size':len(data),'digest':'sha256:'+hashlib.sha256(data).hexdigest(),'browser_download_url':'https://github.com/example/project/releases/download/v9.0.0/update.zip'}
    release={'repository':'example/project','latest':'v9.0.0'}
    return release,asset


def new_installer(state):
    return updater.Installer(SimpleNamespace(store=SimpleNamespace(filename=str(state/'jobs.sqlite3'))),None,None,18766)


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows path API integration')
@pytest.mark.parametrize('state_chars',[185,200,215])
def test_windows_update_stage_accepts_archive_beyond_legacy_max_path(tmp_path,fake_update,state_chars):
    # The stage path itself stays below legacy MAX_PATH; its archive exceeds it.
    desired=state_chars
    state=tmp_path/('s'*max(1,desired-len(str(tmp_path))-1))
    Path(native(state)).mkdir(parents=True)
    installer=new_installer(state)
    installer.fetch(*fake_update)
    assert installer.snapshot()['state']=='ready',installer.snapshot()
    assert installer.prepared
    assert Path(native(installer.prepared['candidate'])).read_bytes().startswith(b'MZ')
    archive=Path(installer.snapshot()['archive'])
    assert len(str(archive).removeprefix('\\\\?\\'))>260


def test_repeated_verified_downloads_retain_bounded_completed_stages(tmp_path,fake_update):
    installer=new_installer(tmp_path)
    for _ in range(6):
        installer.fetch(*fake_update)
        assert installer.snapshot()['state']=='ready'
    # At most current generation and one prior generation.
    stages=[path for path in installer.folder.iterdir() if path.is_dir()]
    assert len(stages)<=2,len(stages)
    assert Path(installer.prepared['candidate']).is_file()

@pytest.mark.skipif(os.name!='nt',reason='Actual Windows path API integration')
def test_copy_update_file_accepts_deep_paths_without_registry_changes(tmp_path):
    folder=tmp_path/('d'*(245-len(str(tmp_path))-1))
    Path(native(folder)).mkdir(parents=True)
    source=folder/'long-candidate-program-file.exe'
    destination=folder/'long-target-program-file.exe'
    data=b'MZ'+b'copy-test'*200
    Path(native(source)).write_bytes(data)
    updater._copy_update_file(source,destination,hashlib.sha256(data).hexdigest())
    assert Path(native(destination)).read_bytes()==data


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows path API integration')
def test_archive_validation_accepts_deep_paths_without_registry_changes(tmp_path):
    folder=tmp_path/('d'*(245-len(str(tmp_path))-1))
    Path(native(folder)).mkdir(parents=True)
    archive=folder/'long-downloaded-release-archive.zip'
    candidate=folder/'long-new-candidate-program.exe'
    data=b'MZ'+b'candidate-test'*200
    with zipfile.ZipFile(native(archive),'w') as payload:
        payload.writestr('YaDiskDownloader/YaDiskDownloader.exe',data)
    updater.validate_archive(archive,candidate)
    assert Path(native(candidate)).read_bytes()==data


def release_prepared(installer):
    if installer._lease is not None:
        installer._lease.close(); installer._lease=None
    installer.prepared=None


@pytest.mark.parametrize('foreign_kind',['legacy','extra','edited','hardlink','malformed','foreign_metadata'])
def test_stage_retention_preserves_unowned_or_changed_content(tmp_path,fake_update,foreign_kind):
    installer=new_installer(tmp_path)
    installer.fetch(*fake_update)
    assert installer.snapshot()['state']=='ready'
    stage=Path(installer.prepared['stage'])
    release_prepared(installer)
    candidate=stage/'new.exe'
    original=candidate.read_bytes()
    canary=None
    if foreign_kind=='legacy':
        (stage/updater.STAGE_MANIFEST).unlink()
    elif foreign_kind=='extra':
        canary=stage/'user-notes.txt';canary.write_bytes(b'foreign user notes')
    elif foreign_kind=='edited':
        candidate.write_bytes(b'Q'*len(original));original=candidate.read_bytes()
    elif foreign_kind=='hardlink':
        canary=tmp_path/'outside-canary.exe';os.link(candidate,canary)
    elif foreign_kind=='malformed':
        (stage/updater.STAGE_MANIFEST).write_text('{invalid',encoding='utf-8')
    elif foreign_kind=='foreign_metadata':
        marker=stage/updater.STAGE_MANIFEST
        metadata=json.loads(marker.read_text(encoding='utf-8'))
        metadata['unrecognized_owner_note']='preserve'
        marker.write_text(json.dumps(metadata),encoding='utf-8')
    try:
        for _ in range(4):
            installer.fetch(*fake_update)
            assert installer.snapshot()['state']=='ready'
        assert stage.is_dir(),foreign_kind
        assert candidate.read_bytes()==original
        if canary is not None:
            assert canary.read_bytes()==(original if foreign_kind=='hardlink' else b'foreign user notes')
    finally:
        release_prepared(installer)


@pytest.mark.parametrize('state',['installing','recovery'])
def test_stage_retention_preserves_parentless_handoff_and_manual_recovery(tmp_path,fake_update,state):
    installer=new_installer(tmp_path);installer.fetch(*fake_update)
    stage=Path(installer.prepared['stage'])
    metadata,_=updater._stage_metadata(stage)
    metadata['state']=state;updater._write_stage(installer._lease,metadata)
    saved=(stage/'new.exe').read_bytes()
    release_prepared(installer)
    try:
        for _ in range(4):installer.fetch(*fake_update)
        assert stage.is_dir() and (stage/'new.exe').read_bytes()==saved
        assert installer.snapshot()['state']=='ready'
    finally:release_prepared(installer)


def test_stage_retention_keeps_newest_completed_rollback_generation(tmp_path,fake_update):
    installer=new_installer(tmp_path);installer.fetch(*fake_update)
    recovery=Path(installer.prepared['stage'])
    metadata,_=updater._stage_metadata(recovery)
    metadata['state']='success';updater._write_stage(installer._lease,metadata)
    release_prepared(installer)
    try:
        for _ in range(5):installer.fetch(*fake_update)
        assert recovery.is_dir()
        assert Path(installer.prepared['candidate']).is_file()
        assert len(list(installer.folder.iterdir()))<=updater.STAGE_RETAIN
    finally:release_prepared(installer)


def test_stage_retention_denied_delete_keeps_old_bytes_and_new_update_ready(tmp_path,fake_update,monkeypatch):
    from disk_downloader import preview_cleanup
    installer=new_installer(tmp_path);installer.fetch(*fake_update)
    preserved=Path(installer.prepared['stage'])
    original=(preserved/'new.exe').read_bytes()
    release_prepared(installer)
    remove=preview_cleanup.remove_owned
    def denied(root,record,*args,**kwargs):
        if Path(root)==preserved:raise PermissionError(errno.EACCES,'synthetic denied cleanup')
        return remove(root,record,*args,**kwargs)
    try:
        with monkeypatch.context() as scope:
            scope.setattr(preview_cleanup,'remove_owned',denied)
            for _ in range(3):installer.fetch(*fake_update)
            assert installer.snapshot()['state']=='ready'
            assert (preserved/'new.exe').read_bytes()==original
        installer.fetch(*fake_update)
        assert installer.snapshot()['state']=='ready'
        assert not preserved.exists()
    finally:release_prepared(installer)


def test_live_separate_helper_lease_protects_stage_after_parent_has_gone(tmp_path,fake_update):
    installer=new_installer(tmp_path);installer.fetch(*fake_update)
    active=Path(installer.prepared['stage'])
    release_prepared(installer)
    ready=tmp_path/'helper-ready';stop=tmp_path/'helper-stop'
    script="""import pathlib,sys,time
from disk_downloader.updater import _StageLease
lease=_StageLease(pathlib.Path(sys.argv[1]))
pathlib.Path(sys.argv[2]).write_text('locked')
try:
    until=time.monotonic()+15
    while not pathlib.Path(sys.argv[3]).exists() and time.monotonic()<until:time.sleep(.02)
finally:lease.close()
"""
    environment=os.environ|{'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(Path(updater.__file__).resolve().parent.parent)}
    process=subprocess.Popen([sys.executable,'-B','-X','utf8','-c',script,str(active),str(ready),str(stop)],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(.02)
        assert ready.exists(),'Synthetic helper did not acquire its OS lease'
        for _ in range(4):installer.fetch(*fake_update)
        assert active.is_dir() and (active/'new.exe').is_file()
        stop.write_text('stop');process.communicate(timeout=5)
        assert process.returncode==0
        installer.fetch(*fake_update)
        assert not active.exists(),'Released old ready stage should become eligible again'
    finally:
        stop.write_text('stop')
        if process.poll() is None:process.terminate()
        process.communicate(timeout=5)
        release_prepared(installer)


def managed_handoff(files):
    lease=updater._StageLease(files.stage,create=True)
    metadata={'format':updater.STAGE_FORMAT,'id':files.stage.name,'archive':'update.zip',
              'created_ns':time.time_ns(),'state':'installing','files':{
                  updater.STAGE_LEASE:lease.record,
                  'new.exe':updater._stage_record(files.candidate),
                  'install.json':updater._stage_record(files.filename)}}
    updater._write_stage(lease,metadata)
    lease.close()


def test_managed_metadata_read_failure_after_shutdown_relaunches_verified_old_target(update_files,launches,monkeypatch):
    managed_handoff(update_files)
    def denied(*args,**kwargs):raise PermissionError(errno.EACCES,'synthetic metadata read denied')
    monkeypatch.setattr(updater,'_stage_metadata',denied)
    updater.apply_update(update_files.filename)
    assert update_files.target.read_bytes()==OLD
    assert len(launches)==1 and Path(launches[0][0])==update_files.target
    result=json.loads((update_files.stage/'result.json').read_text(encoding='utf-8'))
    assert result['ok'] is False and result['restarted'] is True


@pytest.mark.parametrize('succeeds',[True,False])
def test_managed_helper_holds_lease_until_generation_is_sealed(update_files,launches,monkeypatch,succeeds):
    managed_handoff(update_files)
    if succeeds:
        test_confirmed_new_start_preserves_new_generation_history(update_files,launches,monkeypatch)
    else:
        test_failed_new_start_restores_old_program_and_database(update_files,launches,monkeypatch)
    metadata,_=updater._stage_metadata(update_files.stage)
    assert metadata['state']==('success' if succeeds else 'recovery')
    assert metadata['files']['previous.exe']['sha256']==sha(OLD)
    assert metadata['files']['result.json']['sha256']==updater.digest_file(update_files.stage/'result.json')
    lease=updater._StageLease(update_files.stage)
    lease.close()


def unpruned_stages(tmp_path,fake_update,monkeypatch,count=5):
    installer=new_installer(tmp_path)
    with monkeypatch.context() as scope:
        scope.setattr(updater,'_prune_stages',lambda folder:None)
        stages=[]
        for _ in range(count):
            installer.fetch(*fake_update)
            assert installer.snapshot()['state']=='ready'
            stages.append(Path(installer.prepared['stage']))
    release_prepared(installer)
    return installer,stages


def test_stage_cleanup_has_generation_byte_and_time_work_budgets(tmp_path,fake_update,monkeypatch):
    installer,stages=unpruned_stages(tmp_path,fake_update,monkeypatch)
    snapshots={str(path):{child.name:child.read_bytes() for child in path.iterdir()} for path in stages}
    with monkeypatch.context() as scope:
        scope.setattr(updater,'STAGE_PRUNE_SECONDS',0)
        updater._prune_stages(installer.folder)
    with monkeypatch.context() as scope:
        scope.setattr(updater,'STAGE_PRUNE_BYTES',1)
        updater._prune_stages(installer.folder)
    assert snapshots=={str(path):{child.name:child.read_bytes() for child in path.iterdir()} for path in stages}
    with monkeypatch.context() as scope:
        scope.setattr(updater,'STAGE_PRUNE_GENERATIONS',1)
        updater._prune_stages(installer.folder)
        assert len(list(installer.folder.iterdir()))==4
        updater._prune_stages(installer.folder)
        assert len(list(installer.folder.iterdir()))==3
    updater._prune_stages(installer.folder)
    assert len(list(installer.folder.iterdir()))==2


def test_interrupted_owned_stage_cleanup_can_resume_without_removing_foreign_files(tmp_path,fake_update,monkeypatch):
    from disk_downloader import preview_cleanup
    installer,stages=unpruned_stages(tmp_path,fake_update,monkeypatch,count=3)
    oldest=stages[0];metadata,_=updater._stage_metadata(oldest)
    archive=oldest/metadata['archive']
    remove=preview_cleanup.remove_owned
    deleted=[]
    def interrupted(root,record,*args,**kwargs):
        if Path(root)==oldest:
            if deleted:raise TimeoutError('synthetic bounded cleanup interrupted')
            result=remove(root,record,*args,**kwargs)
            if result:deleted.append(record['local'])
            return result
        return remove(root,record,*args,**kwargs)
    with monkeypatch.context() as scope:
        scope.setattr(preview_cleanup,'remove_owned',interrupted)
        updater._prune_stages(installer.folder)
    assert deleted and not archive.exists()
    assert (oldest/'new.exe').is_file() and (oldest/updater.STAGE_MANIFEST).is_file()
    assert updater._stage_metadata(oldest) is not None
    updater._prune_stages(installer.folder)
    assert not oldest.exists()
    assert all((stage/'new.exe').is_file() for stage in stages[1:])


def test_slow_stage_cleanup_does_not_hold_installer_snapshot_lock(tmp_path,fake_update,monkeypatch):
    from disk_downloader import preview_cleanup
    installer,stages=unpruned_stages(tmp_path,fake_update,monkeypatch,count=3)
    entered=threading.Event();release=threading.Event()
    remove=preview_cleanup.remove_owned
    def slow(root,record,*args,**kwargs):
        entered.set()
        assert release.wait(5),'Snapshot test did not release cleanup worker'
        return remove(root,record,*args,**kwargs)
    with monkeypatch.context() as scope:
        scope.setattr(preview_cleanup,'remove_owned',slow)
        worker=threading.Thread(target=installer.fetch,args=fake_update)
        worker.start()
        try:
            assert entered.wait(5)
            started=time.monotonic()
            snapshot=installer.snapshot()
            assert time.monotonic()-started<.5
            assert snapshot['state'] in ('idle','ready','downloading')
        finally:
            release.set();worker.join(5);release_prepared(installer)
        assert not worker.is_alive()


def test_unknown_stage_parser_depth_failure_preserves_files_and_healthy_download(tmp_path,fake_update,monkeypatch):
    installer=new_installer(tmp_path);installer.fetch(*fake_update)
    release_prepared(installer)
    unknown=installer.folder/('f'*32);unknown.mkdir()
    raw=b'['*2000+b']'*2000
    marker=unknown/updater.STAGE_MANIFEST;marker.write_bytes(raw)
    canary=unknown/'user-copy.exe';canary.write_bytes(b'preserved foreign executable bytes')
    parse=json.loads
    def recursion(content,*args,**kwargs):
        if content==raw:raise RecursionError('simulated Python 3.11 parser depth limit')
        return parse(content,*args,**kwargs)
    monkeypatch.setattr(updater.json,'loads',recursion)
    try:
        installer.fetch(*fake_update)
        assert installer.snapshot()['state']=='ready'
        assert Path(installer.prepared['candidate']).is_file()
        assert marker.read_bytes()==raw and canary.read_bytes()==b'preserved foreign executable bytes'
    finally:release_prepared(installer)
