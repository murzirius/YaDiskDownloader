"""Build a native Cocoa launcher and a drag-to-Applications disk image on macOS."""
import argparse
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from disk_downloader import __version__


def bundle_info(architecture):
    if architecture not in ('arm64', 'x86_64'):
        raise ValueError('Supported macOS architectures: arm64 and x86_64')
    return {
        'CFBundleName': 'YaDiskDownloader',
        'CFBundleDisplayName': 'YaDiskDownloader',
        'CFBundleIdentifier': 'com.thomasstudios.yadiskdownloader',
        'CFBundlePackageType': 'APPL',
        'CFBundleExecutable': 'YaDiskDownloader',
        'CFBundleIconFile': 'AppIcon.icns',
        'CFBundleShortVersionString': __version__,
        'CFBundleVersion': __version__,
        'LSMinimumSystemVersion': '13.0',
        'LSArchitecturePriority': [architecture],
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
        'NSHumanReadableCopyright': 'YaDiskDownloader contributors. MIT License.',
    }


def stage_bundle(binary, output, architecture):
    """Refuse to overwrite an existing bundle or follow an input symlink."""
    binary = Path(binary)
    if binary.is_symlink() or not binary.is_file():
        raise ValueError('The native CLI must be a regular file')
    notices = binary.parent / 'THIRD_PARTY_NOTICES.txt'
    if notices.is_symlink() or not notices.is_file():
        raise ValueError('The native CLI license notices are missing')
    info = bundle_info(architecture)
    app = Path(output) / 'YaDiskDownloader.app'
    app.mkdir(parents=True, exist_ok=False)
    contents = app / 'Contents'
    (contents / 'MacOS').mkdir(parents=True)
    (contents / 'Resources').mkdir()
    shutil.copy2(binary, contents / 'MacOS' / 'yadisk')
    (contents / 'MacOS' / 'yadisk').chmod(0o755)
    for source, name in [(ROOT / 'LICENSE', 'LICENSE.txt'), (notices, notices.name), (ROOT / 'docs' / 'macos.md', 'macOS.txt')]:
        shutil.copyfile(source, contents / 'Resources' / name)
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info, sort_keys=True))
    (contents / 'PkgInfo').write_bytes(b'APPL????')
    return app


def run(command):
    subprocess.run([str(part) for part in command], check=True, timeout=180)


def main():
    parser = argparse.ArgumentParser(description='Build macOS .app and .dmg without changing user data')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('dist/macos'))
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build and test this installer on macOS')
    architecture = platform.machine().lower()
    binary = args.binary.absolute()
    # file(1) inspects the actual Mach-O architecture before creating a bundle.
    description = subprocess.check_output(['file', str(binary)], text=True, timeout=10)
    if architecture not in description:
        parser.error('The CLI executable architecture differs from this Mac')
    output = args.output.resolve()
    app = stage_bundle(binary, output / 'image', architecture)
    icon = app / 'Contents' / 'Resources' / 'AppIcon.icns'
    from disk_downloader.tray import image
    image(1024).save(icon, format='ICNS')
    executable = app / 'Contents' / 'MacOS' / 'YaDiskDownloader'
    run(['xcrun', 'clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra', '-Werror',
         '-Wno-unused-parameter', '-Wno-deprecated-declarations', '-mmacosx-version-min=13.0', '-arch', architecture,
         '-framework', 'Cocoa', ROOT / 'native' / 'macos_launcher.m', '-o', executable])
    # The CLI's embedded PyInstaller binaries were signed during its own build.
    # Sign the new launcher, then the outer bundle; do not rewrite nested binaries.
    run(['codesign', '--force', '--sign', '-', executable])
    run(['codesign', '--force', '--sign', '-', app])
    run(['codesign', '--verify', '--deep', '--strict', '--verbose=2', app])
    image_root = app.parent
    (image_root / 'Applications').symlink_to('/Applications', target_is_directory=True)
    (image_root / 'Как установить.txt').write_text(
        'Установка YaDiskDownloader\n\n'
        '1. Перетащите YaDiskDownloader в папку Applications (Программы).\n'
        '2. Закройте этот образ диска.\n'
        '3. Откройте YaDiskDownloader из «Программ».\n\n'
        'Python и команды терминала не нужны. Интерфейс откроется в браузере.\n'
        'Закрытие окна программы не останавливает загрузку. Для завершения нажмите «Выход».\n\n'
        'Тестовая сборка не заверена Apple. Если macOS заблокировала запуск, '
        'откройте «Системные настройки → Конфиденциальность и безопасность» '
        'и разрешите запуск только если доверяете источнику. Защиту системы отключать не нужно.\n',
        encoding='utf-8')
    suffix = 'arm64' if architecture == 'arm64' else 'x64'
    dmg = output / f'YaDiskDownloader-v{__version__}-macOS-{suffix}.dmg'
    if dmg.exists():
        raise FileExistsError(dmg)
    run(['hdiutil', 'create', '-volname', 'YaDiskDownloader', '-srcfolder', image_root,
         '-format', 'UDZO', '-fs', 'HFS+', dmg])
    run(['hdiutil', 'verify', dmg])
    print(dmg.name)


if __name__ == '__main__':
    main()
