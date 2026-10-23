"""The offline performance case must exercise real NetworkWait transitions."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows resource-counter benchmark')
def test_offline_benchmark_waits_for_reconnection(tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root/'tools'/'benchmark_performance.py').is_file():
        root = Path.cwd()
    output = tmp_path/'offline-results'
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(root/'tools'/'benchmark_performance.py'),
                            '--source',str(root),'--output',str(output),'--case','offline','--files','12'],
                            cwd=root, capture_output=True, text=True, encoding='utf-8',timeout=45)
    assert result.returncode == 0, result.stdout+result.stderr
    with sqlite3.connect(output/'state'/'jobs.sqlite3') as db:
        messages=[row[0] for row in db.execute('SELECT message FROM journal_events')]
    assert any('Нет связи' in message for message in messages), messages
    assert any('восстановлена' in message for message in messages), messages
    measured = json.loads((output/'result.json').read_text(encoding='utf-8'))
    validation=measured['offline_validation']
    assert validation['waiting_observed'] is True
    assert validation['successes_before_recovery'] == 0
    assert validation['outage_seconds'] >= 5
    assert measured['correctness']['verified_files'] == 12
