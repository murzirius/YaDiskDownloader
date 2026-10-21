import json
from pathlib import Path

import pytest

from conftest import run_job
from disk_downloader.api import Suspended
from disk_downloader.import_state import find_import, inherit
from disk_downloader.store import Store


@pytest.mark.parametrize('portable',[False,True])
def test_file_list_progress_counts_each_file_once_and_restarts_safely(system,portable):
    engine,api,tmp=system
    for index in range(3):api.file(f'disk:/file-{index}.bin',b'kept',preview=False)
    first=run_job(system,mode='fallback',reports=['json'])
    target=Store(tmp/'restored/jobs.sqlite3') if portable else engine.store
    previous=find_import(target,Path(first['root']))
    with target.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',('restored',json.dumps(first['settings']),first['root'],first['service'],'new',first['started']))
    interrupted=[]
    def stop(done,total,stage):
        interrupted.append((done,total,stage))
        if done:raise Suspended('test stop during inheritance')
    with pytest.raises(Suspended):inherit(target,'restored',previous,details=stop)
    assert interrupted[0][0]==0 and interrupted[-1][0]>0
    progress=[];legacy=[]
    inherit(target,'restored',previous,progress=legacy.append,details=lambda *values:progress.append(values))
    assert progress[0][0]==0 and progress[-1][0]==progress[-1][1]
    assert progress[-1][1]==3
    progress=[item for item in progress if item[1] is not None]
    assert all(0<=done<=total for done,total,_ in progress)
    assert all(a[0]<=b[0] for a,b in zip(progress,progress[1:]))
    assert legacy[-1]==progress[-1][0]
    assert len(list(target.rows("SELECT * FROM artifacts WHERE job='restored'")))==3
    assert len(list(target.rows("SELECT * FROM baseline WHERE job='restored'")))==4
    cache=list(target.rows("SELECT * FROM verification_cache WHERE job='restored'"))
    assert not cache if portable else len(cache)==3
    for index in range(3):assert Path(first['root'],f'file-{index}.bin').read_bytes()==b'kept'
