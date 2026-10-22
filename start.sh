#!/bin/sh
set -eu
if [ "$(uname -s)" != "Linux" ]; then
    echo "Этот способ запуска доступен только в Linux. В macOS откройте приложение из DMG." >&2
    exit 2
fi
cd -- "$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
python_bin=${YADISK_PYTHON:-python3}
"$python_bin" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11 or newer is required"'
if [ ! -x .venv/bin/python ]; then
    "$python_bin" -m venv .venv
fi
required=$(.venv/bin/python -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())')
installed=$(cat .venv/requirements.sha256 2>/dev/null || true)
if [ "$required" != "$installed" ]; then
    if ! .venv/bin/python -m pip install -r requirements.txt; then
        echo "Не удалось установить зависимости. Для первого запуска нужен интернет." >&2
        exit 2
    fi
    printf '%s' "$required" > .venv/requirements.sha256
fi
exec .venv/bin/python run.py "$@"
