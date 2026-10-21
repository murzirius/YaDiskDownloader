import shutil
import subprocess
from pathlib import Path
import pytest

from conftest import test_timeout


def test_theme_schema_and_all_browser_scripts():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is required for browser-script checks')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([node,'--test',str(root/'tests/theme_rules.cjs')],capture_output=True,text=True,timeout=test_timeout(20))
    assert result.returncode==0,result.stdout+result.stderr
    for script in (root/'disk_downloader/static').glob('*.js'):
        result=subprocess.run([node,'--check',str(script)],capture_output=True,text=True,timeout=test_timeout(10))
        assert result.returncode==0,result.stderr
