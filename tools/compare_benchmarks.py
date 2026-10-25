"""Compare like-for-like synthetic scenarios; correctness failures are fatal."""
import argparse
import json
from pathlib import Path

SCENARIOS=('idle','scan','download','pause','offline','missing','zip')


def compare(root):
    rows=[]
    for name in SCENARIOS:
        before=json.loads((root/('before-'+name)/'result.json').read_text(encoding='utf-8'))
        after=json.loads((root/('after-'+name)/'result.json').read_text(encoding='utf-8'))
        for result in (before,after):
            correctness=result['correctness']
            if not correctness.get('sqlite_ok') or any(value is False for value in correctness.values()):
                raise AssertionError('Correctness failed: '+name)
            if result['rss_peak']>512*1024*1024:raise AssertionError('Memory budget exceeded: '+name)
        row={'case':name}
        for key in ('seconds','cpu_seconds','rss_mean','rss_peak','http_p95_ms','sql','io','network_requests'):
            row[key]={'before':before.get(key),'after':after.get(key)}
        rows.append(row)
        if before['seconds']>2 and after['seconds']>before['seconds']*1.3:
            print('::warning::Performance timing needs review: '+name)
    (root/'comparison.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    lines=['| Сценарий | Время до / после, с | CPU до / после, с | Пик RAM до / после, МБ |','|---|---:|---:|---:|']
    for row in rows:
        cell=lambda key,scale=1:' / '.join('%.2f'%(row[key][when]/scale) for when in ('before','after'))
        lines.append('| '+row['case']+' | '+cell('seconds')+' | '+cell('cpu_seconds')+' | '+cell('rss_peak',1024*1024)+' |')
    lines+=['','Время на общих CI-серверах зависит от нагрузки. Предупреждение о скорости требует повторного замера; проверки целостности и предел памяти обязательны.']
    (root/'comparison.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);compare(parser.parse_args().root)
