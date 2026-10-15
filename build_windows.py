import argparse
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def notices(output):
    packages = ("Flask", "requests", "urllib3", "Pillow", "waitress", "pystray", "Send2Trash", "pywin32", "six", "certifi", "charset-normalizer", "idna", "blinker", "click", "itsdangerous", "Jinja2", "MarkupSafe", "PyInstaller")
    texts = ["Third-party components included in YaDiskDownloader.\nThe application license does not replace the licenses below.\n"]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        texts.append("\n===== Python " + sys.version.split()[0] + " =====\n" + python_license.read_text(encoding="utf-8", errors="replace"))
    for name in packages:
        distribution = metadata.distribution(name)
        texts.append("\n===== " + name + " " + distribution.version + " =====\n")
        for file in distribution.files or []:
            if file.name.lower().startswith(("license", "copying", "notice")):
                filename = distribution.locate_file(file)
                if filename.is_file():
                    texts.append("\n--- " + file.name + " ---\n" + filename.read_text(encoding="utf-8", errors="replace"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(texts), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Сборка Windows EXE")
    parser.add_argument("--output", default="dist")
    parser.add_argument("--work", default="build")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Для Windows EXE сборка выполняется на Windows")
    root = Path(__file__).resolve().parent
    output = Path(args.output).resolve()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    from disk_downloader.tray import image
    icon = work / "app.ico"
    image().save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed", "--noupx", "--name", "YaDiskDownloader", "--icon", str(icon), "--distpath", str(output), "--workpath", str(work / "cache"), "--specpath", str(work), "--hidden-import", "pystray._win32", "--add-data", str(root / "disk_downloader" / "templates") + ";disk_downloader/templates", "--add-data", str(root / "disk_downloader" / "static") + ";disk_downloader/static", str(root / "run.py")]
    subprocess.run(command, cwd=root, check=True)
    notices(output)


if __name__ == "__main__":
    main()
