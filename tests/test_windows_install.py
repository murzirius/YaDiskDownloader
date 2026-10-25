import json
from pathlib import Path

import pytest
from disk_downloader.windows_install import install_files, required_files


def payload(root):
    for name in required_files():
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(('Synthetic '+name).encode())


def test_install_preserves_history_and_requires_ownership(tmp_path):
    source,target=tmp_path/'source',tmp_path/'Programs/App'
    payload(source)
    history=tmp_path/'history.sqlite3';history.write_bytes(b'private history')
    install_files(source,target)
    for name in required_files():assert (target/name).read_bytes()==(source/name).read_bytes()
    assert history.read_bytes()==b'private history'
    (source/'YaDiskDownloader.exe').write_bytes(b'updated synthetic executable')
    install_files(source,target)
    assert (target/'YaDiskDownloader.exe').read_bytes()==b'updated synthetic executable'
    (target/'YaDiskDownloader.exe').write_bytes(b'foreign executable')
    with pytest.raises(ValueError):install_files(source,target)
    assert (target/'YaDiskDownloader.exe').read_bytes()==b'foreign executable'


def test_incomplete_archive_cannot_modify_installed_program(tmp_path):
    source,target=tmp_path/'source',tmp_path/'app'
    payload(source);install_files(source,target)
    before=(target/'YaDiskDownloader.exe').read_bytes()
    (source/'LICENSE').unlink()
    with pytest.raises(ValueError):install_files(source,target)
    assert (target/'YaDiskDownloader.exe').read_bytes()==before


def test_copy_failure_rolls_back_previous_application(tmp_path,monkeypatch):
    import disk_downloader.windows_install as module
    source,target=tmp_path/'source',tmp_path/'app'
    payload(source);install_files(source,target)
    before=(target/'YaDiskDownloader.exe').read_bytes()
    (source/'YaDiskDownloader.exe').write_bytes(b'new executable')
    replace=module.os.replace
    def fail(src,dst):
        if Path(dst).name=='LICENSE' and 'backup' not in str(src):raise OSError('Synthetic disk failure')
        return replace(src,dst)
    monkeypatch.setattr(module.os,'replace',fail)
    with pytest.raises(OSError):install_files(source,target)
    assert (target/'YaDiskDownloader.exe').read_bytes()==before
    assert not list(target.parent.glob('.yadisk-install-*'))


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows Shell shortcut')
def test_real_windows_shortcut_is_created_in_isolated_menu(tmp_path):
    from disk_downloader.windows_install import shortcut
    target,menu=tmp_path/'installed',tmp_path/'menu'
    target.mkdir();menu.mkdir()
    (target/'YaDiskDownloader.exe').write_bytes(b'MZ synthetic shortcut target')
    shortcut(target,menu_dir=menu)
    link=menu/'YaDiskDownloader.lnk'
    assert link.is_file() and link.stat().st_size>100
    shortcut(target,menu_dir=menu)
    assert len(list(menu.iterdir()))==1
