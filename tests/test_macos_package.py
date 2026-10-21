import importlib.util
from pathlib import Path
import plistlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('macos_builder', ROOT / 'tools' / 'build_macos_app.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize('architecture', ['arm64', 'x86_64'])
def test_bundle_preserves_cli_and_includes_installable_metadata(tmp_path, architecture):
    native = tmp_path / 'native'
    native.mkdir()
    binary = native / 'yadisk'
    binary.write_bytes(b'example native executable\x00\xff')
    (native / 'THIRD_PARTY_NOTICES.txt').write_text('Dependency licenses', encoding='utf-8')
    app = builder.stage_bundle(binary, tmp_path / 'package', architecture)
    contents = app / 'Contents'
    info = plistlib.loads((contents / 'Info.plist').read_bytes())
    assert info['CFBundleExecutable'] == 'YaDiskDownloader'
    assert info['CFBundlePackageType'] == 'APPL'
    assert info['NSHighResolutionCapable'] is True
    assert info['LSArchitecturePriority'] == [architecture]
    assert (contents / 'MacOS' / 'yadisk').read_bytes() == binary.read_bytes()
    assert (contents / 'Resources' / 'THIRD_PARTY_NOTICES.txt').read_text() == 'Dependency licenses'
    assert (contents / 'Resources' / 'LICENSE.txt').is_file()
    with pytest.raises(FileExistsError):
        builder.stage_bundle(binary, tmp_path / 'package', architecture)
    assert binary.read_bytes() == b'example native executable\x00\xff'


def test_packaging_rejects_unknown_architecture_and_missing_notices(tmp_path):
    with pytest.raises(ValueError, match='architectures'):
        builder.bundle_info('unsupported')
    binary = tmp_path / 'yadisk'
    binary.write_bytes(b'executable')
    with pytest.raises(ValueError, match='notices'):
        builder.stage_bundle(binary, tmp_path / 'out', 'arm64')
    assert not (tmp_path / 'out').exists()


def test_native_installation_docs_do_not_disable_gatekeeper():
    doc = (ROOT / 'docs' / 'macos.md').read_text(encoding='utf-8')
    assert 'не заверена Apple' in doc
    assert 'spctl --master-disable' not in doc and 'xattr -cr' not in doc
