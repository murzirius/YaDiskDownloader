"""Run baseline/current on the same runner; never contacts a cloud account."""
import argparse
import io
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile
from compare_benchmarks import compare, SCENARIOS


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--baseline',default=os.environ.get('PERFORMANCE_BASE',''));args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='yadisk-perf-base-') as folder:
        baseline=Path(folder)
        revision=args.baseline or 'HEAD^'
        if revision and set(revision)=={'0'}:revision='HEAD^'
        if revision!='HEAD^':
            if not re.fullmatch('[0-9a-f]{40,64}',revision):raise ValueError('Invalid baseline revision')
            subprocess.run(['git','fetch','--quiet','--depth','1','origin',revision],cwd=root,check=True)
        data=subprocess.run(['git','archive','--format=zip',revision],cwd=root,check=True,capture_output=True).stdout
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for item in archive.infolist():
                path=PurePosixPath(item.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in item.filename:raise ValueError('Invalid baseline path')
            archive.extractall(baseline)
        import json
        import statistics
        for case in SCENARIOS:
            for trial in range(3):
                variants=[('before',baseline),('after',root)]
                if trial % 2:variants.reverse()
                for label,source in variants:
                    count=5000 if case=='scan' else 300
                    destination=out/(label+'-'+case+'-'+str(trial))
                    subprocess.run([sys.executable,'-B','tools/benchmark_performance.py','--source',str(source),'--output',str(destination),'--case',case,'--files',str(count),'--seconds','6','--test-client'],cwd=root,check=True,stdout=subprocess.DEVNULL)
            for label in ('before','after'):
                results=[json.loads((out/(label+'-'+case+'-'+str(trial))/'result.json').read_text()) for trial in range(3)]
                result=dict(results[0])
                for key in ('seconds','cpu_seconds','rss_mean','rss_peak','http_p95_ms'):
                    result[key]=statistics.median(row[key] for row in results)
                result['repetitions']=[{k:r[k] for k in ('seconds','cpu_seconds','rss_peak')} for r in results]
                destination=out/(label+'-'+case);destination.mkdir()
                (destination/'result.json').write_text(json.dumps(result),encoding='utf-8')
                print('Measured:',label,case,flush=True)
        compare(out)


if __name__=='__main__':main()
