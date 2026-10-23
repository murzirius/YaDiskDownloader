"""Public archives must contain tracked project material, never local canaries."""
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from tools import package_release


DOCS = ('docs/usage.md', 'docs/features-v1.3.0.md', 'docs/cli.md',
        'docs/features-v1.5.0.md', 'docs/validation-v1.5.0.md',
        'docs/scan-pipeline-validation.md', 'docs/validation-storage-v1.5.0.md',
        'docs/macos.md', 'docs/architecture.md', 'docs/testing.md')


@pytest.mark.parametrize('tag,expected', [
    ('v'+package_release.VERSION, True),
    ('v'+package_release.VERSION+'-build.'+package_release.BUILD, True),
    ('v'+package_release.VERSION+'-build.19000101.1', False),
    ('v9.9.9-build.'+package_release.BUILD, False),
    (None, False),
    ('v'+package_release.VERSION+'-build.'+package_release.BUILD+'-other', False),
])
def test_release_tag_matches_exact_application_version_and_build(tag, expected):
    assert package_release.valid_release_tag(tag) is expected


def git(root, *args, input=None):
    return subprocess.run(['git', '-C', str(root), *args], input=input,
                          capture_output=True, check=True).stdout


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    public = {'README.md': 'Public manual', 'LICENSE': 'Project license',
              '.gitignore': '# Synthetic project\n',
              'requirements.txt': 'ExampleDependency==1.0',
              'disk_downloader/__init__.py': '__version__ = "1.5.0"',
              'disk_downloader/loader.py': '# Public loader',
              'docs/public.md': 'Public documentation',
              'tools/public.py': '# Public tool',
              'tests/test_public.py': '# Public tests',
              'native/launcher.m': '// Public native source',
              '.github/workflows/checks.yml': 'name: Public checks'}
    for name in (*DOCS, 'docs/release-v' + package_release.VERSION + '.md'):
        public[name] = 'Public release documentation'
    private = ('docs/AGENTS.md', 'docs/CLAUDE.MD', 'docs/GEMINI.md',
               'docs/SKILL.md', 'tools/AGENTS.override.md',
               '.github/copilot-instructions.md', 'docs/.codex/notes.md',
               'tools/.claude/local.py', 'docs/.local/settings.txt',
               'disk_downloader/.state/session.txt', 'docs/private/notes.md',
               'tools/local_settings.py', 'docs/tokens.txt')
    for name, text in public.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding='utf-8')
    for name in private:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('PRIVATE-CANARY-' + name, encoding='utf-8')
    git(root, 'init', '--quiet')
    # Test repositories need an index, never fixture commits or author changes.
    git(root, 'add', '--force', '--', *public, *private)
    untracked = root / 'disk_downloader' / 'local_debug.py'
    untracked.write_text('UNTRACKED-CANARY', encoding='utf-8')
    monkeypatch.setattr(package_release, 'ROOT', root)
    binary = tmp_path / 'binary' / 'YaDiskDownloader.exe'
    binary.parent.mkdir()
    binary.write_bytes(b'MZ' + b'synthetic executable' * 100)
    (binary.parent / 'THIRD_PARTY_NOTICES.txt').write_text('Dependency licenses', encoding='utf-8')
    wheel = binary.parent / 'third_party' / 'pystray-0.19.5-py2.py3-none-any.whl'
    wheel.parent.mkdir()
    wheel.write_bytes(b'synthetic original dependency wheel')
    return root, public, private, binary


def test_source_selection_excludes_recursive_private_and_untracked_files(source_tree):
    root, public, private, _ = source_tree
    names = {file.relative_to(root).as_posix() for file in package_release.source_files()}
    assert names == set(public)
    assert not names.intersection(private)
    assert 'disk_downloader/local_debug.py' not in names


def test_actual_nested_distribution_preserves_licenses_without_private_canaries(source_tree, tmp_path):
    root, public, _, binary = source_tree
    archives = package_release.package(tmp_path / 'release', binary)
    assert len(archives) == 1
    assert list((tmp_path / 'release').iterdir()) == archives
    with zipfile.ZipFile(archives[0]) as outer:
        assert outer.testzip() is None
        assert outer.read('YaDiskDownloader/YaDiskDownloader.exe') == binary.read_bytes()
        assert outer.read('YaDiskDownloader/LICENSE') == b'Project license'
        assert outer.read('YaDiskDownloader/THIRD_PARTY_NOTICES.txt') == b'Dependency licenses'
        assert any(name.endswith('.whl') for name in outer.namelist())
        source = next(name for name in outer.namelist() if '/source/' in name)
        with zipfile.ZipFile(io.BytesIO(outer.read(source))) as nested:
            assert nested.testzip() is None
            assert set(nested.namelist()) == {'disk-downloader/' + name for name in public}
            for name in nested.namelist():
                assert b'PRIVATE-CANARY' not in nested.read(name)
                assert b'UNTRACKED-CANARY' not in nested.read(name)


def test_parent_git_repository_never_authorizes_a_nested_source_root(source_tree, monkeypatch):
    parent, _, _, _ = source_tree
    nested = parent / 'nested'
    nested.mkdir()
    (nested / 'README.md').write_text('Unrelated nested project', encoding='utf-8')
    git(parent, 'add', '--', 'nested/README.md')
    monkeypatch.setattr(package_release, 'ROOT', nested)
    with pytest.raises(ValueError):
        list(package_release.source_files())


def test_git_unavailable_fails_closed(source_tree, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('Synthetic missing git executable')

    monkeypatch.setattr(subprocess, 'run', unavailable)
    with pytest.raises(ValueError):
        list(package_release.source_files())


def test_unmerged_git_index_cannot_be_packaged(source_tree):
    root, _, _, _ = source_tree
    oid = git(root, 'hash-object', '-w', '--stdin', input=b'synthetic conflict').strip()
    path = b'disk_downloader/loader.py'
    stages = b'0 ' + b'0' * 40 + b'\t' + path + b'\n'
    stages += b'100644 ' + oid + b' 1\t' + path + b'\n'
    stages += b'100644 ' + oid + b' 2\t' + path + b'\n'
    git(root, 'update-index', '--index-info', input=stages)
    with pytest.raises(ValueError):
        list(package_release.source_files())


def test_required_outer_document_must_be_tracked(source_tree, tmp_path):
    root, _, _, binary = source_tree
    git(root, 'rm', '--cached', '--', 'docs/usage.md')
    with pytest.raises(ValueError):
        package_release.package(tmp_path / 'release', binary)
    assert not list((tmp_path / 'release').glob('*.zip'))


def test_linked_source_is_not_followed_or_published(source_tree, tmp_path):
    root, _, _, _ = source_tree
    outside = tmp_path / 'outside.py'
    outside.write_text('PRIVATE-OUTSIDE-CANARY', encoding='utf-8')
    linked = root / 'disk_downloader' / 'linked.py'
    try:
        os.link(outside, linked)
    except OSError:
        pytest.skip('Filesystem does not support hardlinks')
    git(root, 'add', '--', 'disk_downloader/linked.py')
    assert linked not in set(package_release.source_files())
    assert outside.read_text(encoding='utf-8') == 'PRIVATE-OUTSIDE-CANARY'


def test_outer_document_link_cannot_bypass_source_filter(source_tree, tmp_path):
    root, _, _, binary = source_tree
    outside = tmp_path / 'outside.md'
    outside.write_text('PRIVATE-OUTSIDE-CANARY', encoding='utf-8')
    document = root / 'docs' / 'usage.md'
    document.unlink()
    os.link(outside, document)
    with pytest.raises(ValueError):
        package_release.package(tmp_path / 'release', binary)
    assert outside.read_text(encoding='utf-8') == 'PRIVATE-OUTSIDE-CANARY'
