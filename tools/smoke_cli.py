"""Run a real console distribution in a temporary state without cloud access."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from disk_downloader.service_control import Client


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    exe=args.exe.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='yadisk-cli-smoke-') as folder:
        state=Path(folder)/'state'
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        command=[str(exe),'--port',str(port),'--state-dir',str(state),'--json']
        result=subprocess.run(command+['start'],capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stderr
        process=None
        try:
            import psutil
            process=psutil.Process(json.loads(result.stdout)['pid'])
            client=Client(port,state)
            runtime=client.call('/api/runtime');assert runtime['frozen']
            response=client.session.get(client.base+'/static/file-icons.svg',timeout=5)
            assert response.ok and b'<symbol id="folder"' in response.content, (response.status_code,response.content[:150])
            status=subprocess.run(command+['status'],capture_output=True,text=True,timeout=15)
            assert status.returncode==0 and json.loads(status.stdout)['jobs']==[]
            assert process.is_running()
            client.call('/api/exit',{})
            try:process.wait(timeout=25)
            except psutil.TimeoutExpired:raise AssertionError('Console distribution did not shut down')
            with socket.socket() as probe:assert probe.connect_ex(('127.0.0.1',port))!=0
            result={'actual_binary':True,'version':runtime['version'],'build':runtime['build'],'detached_start':True,'after_launcher_exit':True,'status_command':True,'svg_resources':True,'full_exit':True,'system':sys.platform}
            args.output.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
        finally:
            if process and process.is_running():
                children=process.children(recursive=True)
                for child in reversed(children):
                    if child.is_running():child.terminate()
                process.terminate()
                try:process.wait(timeout=5)
                except Exception:process.kill();process.wait(timeout=5)


if __name__=='__main__':main()
