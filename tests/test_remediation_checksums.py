"""Equal-length corruption must be rejected by each independent checksum guard."""
import hashlib
import json
from pathlib import Path
import pytest
from conftest import make_job, wait_job


@pytest.mark.parametrize('hashes', [('sha256',), ('md5',), ('sha256','md5')], ids=['sha-only','md5-only','both'])
@pytest.mark.parametrize('case', ['valid','content','metadata'])
def test_original_equal_length_checksum_validation(system, hashes, case):
    engine, api, _ = system
    path='disk:/checksum.bin'
    body=b'original'
    api.file(path,data=body,preview=False,media_type='document',mime_type='application/octet-stream')
    meta=api.tree[path]
    for key in ('sha256','md5'):
        if key not in hashes:meta.pop(key)
    if case == 'content':
        api.data[(path,'original')]=b'originaX'
        assert len(api.data[(path,'original')]) == meta['size']
    elif case == 'metadata':
        # In the both case SHA is correct, independently exercise MD5 too.
        key=hashes[-1]
        meta[key]='0'*len(meta[key])
    job_id=make_job(system,mode='fallback')
    settings=engine.store.job(job_id)['settings']
    settings['attempts']=1
    engine.store.update_job(job_id,settings=json.dumps(settings))
    engine.action(job_id,'start')
    job=wait_job(engine,job_id)
    artifacts=list(engine.store.rows('SELECT * FROM artifacts WHERE job=?',(job['id'],)))
    if case == 'valid':
        assert job['phase']=='completed' and job['errors']==0
        assert len(artifacts)==1
        content=Path(job['root'],artifacts[0]['local']).read_bytes()
        assert content==body and artifacts[0]['sha256']==hashlib.sha256(body).hexdigest()
    else:
        assert job['phase']=='completed_with_errors' and job['errors']==1
        assert job['saved_bytes']==0 and artifacts==[]
        assert api.media_calls==[(path,'original')]
    assert not list(Path(job['root']).rglob('*.part'))
    with engine.store.db() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
