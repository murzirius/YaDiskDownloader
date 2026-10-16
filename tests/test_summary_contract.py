import pytest
from disk_downloader.store import Store
from performance_fixtures import seed_summary
from summary_reference import EXPECTED

@pytest.mark.parametrize('label',list(EXPECTED))
def test_summary_matches_preoptimization_contract(tmp_path,label):
    mode,operation,generation=('fallback','new','0') if label=='empty' else label.split('-')
    store=Store(tmp_path/'state.sqlite3')
    key=seed_summary(store,tmp_path,mode,operation,int(generation),empty=label=='empty')
    actual=store.summary(key)
    assert {key:actual[key] for key in EXPECTED[label]}==EXPECTED[label]


def test_summary_does_not_enter_the_writer_gate(tmp_path,monkeypatch):
    store=Store(tmp_path/'state.sqlite3')
    key=seed_summary(store,tmp_path,'fallback','new',0)
    def forbidden(owner):raise AssertionError('statistics attempted to take writer admission')
    monkeypatch.setattr(store.write_gate,'acquire',forbidden)
    assert store.summary(key)['files']>0


def test_legacy_null_status_is_not_counted_as_pending(tmp_path):
    import json
    store=Store(tmp_path/'state.sqlite3')
    key=seed_summary(store,tmp_path,'fallback','new',0,empty=True)
    meta={'path':'disk:/legacy.jpg','name':'legacy.jpg','type':'file',
          'size':None,'file_type':'photos','has_preview':True}
    with store.db() as db:
        db.execute('INSERT INTO entries(job,path,kind,meta,status) VALUES(?,?,?,?,NULL)',
                   (key,meta['path'],'file',json.dumps(meta)))
    assert store.summary(key)['remaining_work']=={
        'bytes':0,'unknown_files':0,'files':0,'preview_files':0}
