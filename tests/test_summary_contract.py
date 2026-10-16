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
