"""File-focused preparation must not perform filesystem IO for history rows."""
import json
from pathlib import Path

import pytest

from conftest import run_job, wait_job
from disk_downloader.engine import Control
from disk_downloader.import_state import find_import, inherit
from disk_downloader.store import Store


def target_job(store, first, identity='next'):
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   (identity,json.dumps(first['settings']),first['root'],first['service'],'new',first['started']))


@pytest.mark.parametrize('portable',[False,True])
def test_preparation_counts_files_not_history_and_never_checks_history_paths(system,monkeypatch,portable):
    engine,api,tmp=system
    api.file('disk:/kept.jpg')
    first=run_job(system,mode='both')
    with engine.store.db() as db:
        db.executemany('INSERT INTO names VALUES(?,?,?,?)',
            [(first['id'],f'@output:old-{i}',f'old-logs/{i}.html',f'old-logs/{i}.html') for i in range(1200)])
        db.execute('INSERT INTO verification_cache VALUES(?,?,?)',(first['id'],'old-logs/orphan.html','untrusted'))
    if portable:
        from disk_downloader.import_state import save_import
        save_import(engine.store,first['id'])
    target=Store(tmp/'portable/jobs.sqlite3') if portable else engine.store
    previous=find_import(target,Path(first['root']))
    target_job(target,first)
    def forbidden(*args,**kwargs):
        raise AssertionError('History metadata must not inspect destination paths')
    monkeypatch.setattr('disk_downloader.import_state.safe_join',forbidden)
    progress=[]
    inherit(target,'next',previous,details=lambda *item:progress.append(item))
    assert progress[-1]==(1,1,'files')  # both copies still count as ONE photo
    with target.db() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE job='next'").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM names WHERE job='next' AND identity LIKE '@output:%'").fetchone()[0]>=1200
        assert not db.execute("SELECT 1 FROM verification_cache WHERE job='next' AND local LIKE 'old-logs/%'").fetchone()
        if portable:assert not db.execute("SELECT 1 FROM verification_cache WHERE job='next'").fetchone()


def test_cached_files_do_not_run_preview_cleanup_without_preview(system,monkeypatch):
    engine,api,_=system
    for i in range(30):api.file(f'disk:/{i}.bin',b'kept',preview=False)
    first=run_job(system,mode='fallback')
    job=engine.store.job(first['id']);job['settings']['operation']='missing'
    queries=[];connect=engine.store._connect
    def measured():
        connection=connect();connection.set_trace_callback(queries.append);return connection
    monkeypatch.setattr(engine.store,'_connect',measured)
    def forbidden(*args):raise AssertionError('No preview exists to remove')
    monkeypatch.setattr(engine,'remove_preview',forbidden)
    engine.check_local_results(job,Control())
    assert len([sql for sql in queries if sql.startswith('SELECT')])<15
    assert engine.store.summary(first['id'])['originals']==30


def test_find_import_does_not_resolve_unrelated_history_roots(system,monkeypatch):
    engine,api,_=system
    api.file('disk:/kept.bin',b'kept',preview=False)
    first=run_job(system,mode='fallback')
    settings=json.dumps(first['settings'])
    old=Path(first['root']).parent/'disconnected-old-NAS'
    with engine.store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started) VALUES(?,?,?,?,?,?)',
                   ('old',settings,str(old),'service','completed','2099-01-01'))
    resolve=Path.resolve
    def guarded(path,*args,**kwargs):
        assert path!=old,'Unrelated old folder must not delay the current download'
        return resolve(path,*args,**kwargs)
    monkeypatch.setattr(Path,'resolve',guarded)
    assert find_import(engine.store,Path(first['root']))['id']==first['id']


def test_removed_and_changed_files_keep_change_report_and_collision_safe_names(system):
    engine,api,_=system
    api.file('disk:/same?.bin',b'old',preview=False)
    api.file('disk:/same*.bin',b'other',preview=False)
    first=run_job(system,mode='fallback')
    before={row['path']:row['local'] for row in engine.store.rows('SELECT * FROM artifacts WHERE job=?',(first['id'],))}
    api.file('disk:/same?.bin',b'new',preview=False)
    # Keep the second copy intact while replacing a changed source.
    job_id=engine.create({'operation':'missing','destination':first['root']})
    ready=wait_job(engine,job_id)
    assert ready['phase']=='ready'
    with engine.store.db() as db:
        assert db.execute('SELECT status FROM changes WHERE job=? AND path=?',(job_id,'disk:/same?.bin')).fetchone()[0]=='changed'
        assert db.execute('SELECT local FROM names WHERE job=? AND identity=?',(job_id,'disk:/same*.bin')).fetchone()[0]==before['disk:/same*.bin']
    for path,data in [('disk:/same?.bin',b'old'),('disk:/same*.bin',b'other')]:
        assert (Path(first['root'])/before[path]).read_bytes()==data


def test_history_import_does_not_deadlock_in_legacy_cmd_rollback_mode(tmp_path,monkeypatch):
    import hashlib
    monkeypatch.setattr('disk_downloader.store.journal_mode',lambda:'DELETE')
    store=Store(tmp_path/'state.sqlite3')
    settings={'paths':['disk:/'],'destination':str(tmp_path)}
    first={'root':str(tmp_path),'settings':settings,'service':'service','started':'2026-01-01'}
    target_job(store,first,'source');target_job(store,first,'target')
    sha=hashlib.sha256(b'kept').hexdigest()
    with store.db() as db:
        for i in range(450):
            path=f'disk:/{i}.bin';local=f'{i}.bin'
            db.execute('INSERT INTO entries(job,path,kind,meta) VALUES(?,?,?,?)',('source',path,'file',json.dumps({'path':path,'type':'file','size':4})))
            db.execute('INSERT INTO names VALUES(?,?,?,?)',('source',path,local,local))
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',('source',path,'original',local,4,sha,'fixture',None))
            db.execute('INSERT INTO verification_cache VALUES(?,?,?)',('source',local,'[]'))
    assert not store.wal
    inherit(store,'target',{'store':store,'id':'source','job':store.job('source')})
    with store.db() as db:
        for table in ('baseline','names','artifacts','verification_cache'):
            assert db.execute('SELECT COUNT(*) FROM '+table+" WHERE job='target'").fetchone()[0]==450


def test_interrupted_portable_import_keeps_its_selected_manifest(system,monkeypatch):
    from disk_downloader.engine import Engine
    engine,api,tmp=system
    api.file('disk:/kept.bin',b'kept',preview=False)
    first=run_job(system,mode='fallback')
    restored=Engine(Store(tmp/'fresh/jobs.sqlite3'),api)
    launch=restored.launch
    monkeypatch.setattr(restored,'launch',lambda *args:None)
    job_id=restored.create({'operation':'missing','destination':first['root']})
    assert 'manifest' in restored.store.job(job_id)['settings']['_inherit_source']
    def forbidden(*args,**kwargs):raise AssertionError('The saved manifest must not be reselected from unrelated history')
    monkeypatch.setattr('disk_downloader.engine.find_import',forbidden)
    launch(job_id,'manifest')
    result=wait_job(restored,job_id)
    assert result['phase']=='stopped' and not result['settings'].get('_inherit_source')
    with restored.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts WHERE job=?',(job_id,)).fetchone()[0]==1
    assert (Path(first['root'])/'kept.bin').read_bytes()==b'kept'


def test_browser_counters_show_files_instead_of_history_records():
    import shutil
    import subprocess
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is unavailable')
    script=Path('disk_downloader/static/app.js').read_text(encoding='utf-8')
    function=script[script.index('function renderJobCounters('):script.index('function renderEstimate(')]
    fixture="""
const assert=require('node:assert/strict'),rows=[];
const $=()=>({replaceChildren(){}}),bytes=value=>String(value),counter=(holder,label,value)=>rows.push([label,value]);
const job={phase:'preparing',active:[{unit:'local_files',done:55506,size:55506}],speed:0,
completeness:{originals:0,preview_only:0,no_result:0},workers_status:{current:0,maximum:8},counts:{},errors:0,scan_errors:0};
"""
    assertions="""
renderJobCounters(job);
assert.equal(rows[0][0],'Скачанные файлы');
assert.ok(rows[0][1].endsWith('файлов'));
assert.ok(!rows.some(row=>String(row[1]).includes('запис')));
rows.length=0;job.active[0].size=null;renderJobCounters(job);
assert.equal(rows[0][1],'Готовим проверку…');
"""
    subprocess.run([node,'-e',fixture+function+assertions],check=True,timeout=10)


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows device path URI encoding')
def test_portable_inventory_uses_sqlite_uri_compatible_with_python_311(system,monkeypatch):
    from urllib.parse import quote_from_bytes
    from disk_downloader.import_state import read_import
    engine,api,_=system
    api.file('disk:/kept.bin',b'kept',preview=False)
    first=run_job(system,mode='fallback')
    manifest=engine.store.job(first['id'])['settings']['manifest_local']
    def legacy_uri(path):
        # CPython 3.11 pathlib._WindowsFlavour.make_uri semantics.
        drive=path.drive
        if len(drive)==2 and drive[1]==':':
            return 'file:///%s/%s'%(drive,quote_from_bytes(path.as_posix()[2:].lstrip('/').encode('utf-8')))
        return 'file:'+quote_from_bytes(path.as_posix().encode('utf-8'))
    monkeypatch.setattr(Path,'as_uri',legacy_uri)
    previous=read_import(first['root'],manifest)
    assert previous['inventory_available']
    assert previous['job']['settings']['paths']==['disk:/']
