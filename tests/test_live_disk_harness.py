import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tools.check_live_disk import validate_manifest
from tools.create_cloud_fixtures import create


@pytest.mark.parametrize('path', ['../outside', '/outside', 'a/../outside', 'a//b', 'C:/file', 'a\\b', '.', '', 'x\0y'])
def test_manifest_cannot_escape_fixture_folder(path):
    with pytest.raises(ValueError):
        validate_manifest({'format':1,'files':[{'path':path,'size':1,'sha256':'a'*64}]})


def test_fixture_generator_has_no_personal_data_and_verifiable_hashes(tmp_path):
    root=tmp_path/'fixtures'
    assert create(root) == 31
    files=validate_manifest(json.loads((root/'fixture-manifest.json').read_text(encoding='utf-8')))
    assert (root/'empty-folder').is_dir()
    assert sum(f['size'] for f in files) < 20*1024*1024
    for item in files:
        with (root/item['path']).open('rb') as stream:
            assert hashlib.file_digest(stream,'sha256').hexdigest() == item['sha256']
    with pytest.raises(FileExistsError):create(root)


def test_missing_token_fails_closed_without_cloud_access(tmp_path):
    env={key:value for key,value in os.environ.items() if key!='YADISK_TEST_TOKEN'}
    output=tmp_path/'report.json'
    result=subprocess.run([sys.executable,'-B','tools/check_live_disk.py','--output',str(output)],env=env,capture_output=True,text=True)
    assert result.returncode == 2
    assert json.loads(output.read_text())['status'] == 'not_configured'


def test_manifest_bounds_and_duplicate_names():
    record={'path':'one.txt','size':1,'sha256':'a'*64}
    for files in [[],[record]*101,[record,record|{'path':'ONE.txt'}],[record|{'size':65*1024*1024}],[record|{'sha256':'bad'}],[record|{'size':True}]]:
        with pytest.raises(ValueError):validate_manifest({'format':1,'files':files})


def test_live_fixture_guard_rejects_unknown_files_before_download(monkeypatch):
    from tools import check_live_disk as live
    from disk_downloader.api import APIError, Credentials, DiskAPI
    credentials=Credentials();credentials.set('synthetic-token')
    api=live.TestAPI(credentials)
    api.allowed_files={live.PREFIX+'/known.txt'}
    monkeypatch.setattr(DiskAPI,'metadata',lambda *args,**kwargs:{'path':live.PREFIX,'type':'dir','_embedded':{'items':[{'path':live.PREFIX+'/unknown.txt','type':'file'}]}})
    with pytest.raises(APIError):api.metadata(live.PREFIX)
    with pytest.raises(ValueError):api.metadata('disk:/private')
