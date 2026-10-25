"""Compare JSON transport with a local controlled HTTP server, without Yandex."""
import argparse
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import types
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import disk_downloader.api as current


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--baseline',default='HEAD^');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    original=subprocess.run(['git','show',args.baseline+':disk_downloader/api.py'],cwd=root,check=True,capture_output=True).stdout.decode('utf-8')
    baseline=types.ModuleType('disk_downloader._baseline_transport');baseline.__package__='disk_downloader'
    exec(compile(original,'baseline-api.py','exec'),baseline.__dict__)
    stats={'connections':0,'wire_body_bytes':0}
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def get_request(self):
            sock,addr=super().get_request();sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            stats['connections']+=1
            return sock,addr
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            selected=bool(parse_qs(urlsplit(self.path).query).get('fields'))
            items=[]
            for i in range(100):
                item=dict(path='disk:/file%d.bin'%i,name='file%d.bin'%i,type='file',size=1024,sha256='%064x'%i,modified='2026-01-01T00:00:00Z')
                if not selected:item.update(custom_properties={'description':'Synthetic unused metadata '*10},public_url='https://example.invalid/public-link')
                items.append(item)
            content=json.dumps(dict(path='disk:/',name='Disk',type='dir',_embedded={'items':items,'total':100})).encode()
            compressed=self.headers.get('Accept-Encoding')=='gzip'
            if compressed:content=gzip.compress(content)
            stats['wire_body_bytes']+=len(content)
            self.send_response(200)
            self.send_header('Content-Length',str(len(content)))
            if compressed:self.send_header('Content-Encoding','gzip')
            self.end_headers();self.wfile.write(content)
    server=Server(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    rows=[]
    try:
        for trial in range(3):
            for label,module in ([('before',baseline),('after',current)] if trial%2==0 else [('after',current),('before',baseline)]):
                module.BASE='http://127.0.0.1:%d'%server.server_port
                credentials=module.Credentials();credentials.set('synthetic-token')
                api=module.DiskAPI(credentials)
                start=dict(stats);started=time.perf_counter();cpu=time.process_time()
                try:
                    for _ in range(20):
                        assert len(api.metadata('disk:/')['_embedded']['items'])==100
                    rows.append(dict(trial=trial,variant=label,requests=20,seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,**{k:stats[k]-start[k] for k in stats}))
                finally:
                    credentials.clear()
                    if hasattr(api,'json_connections'):api.json_connections.close_idle()
    finally:
        server.shutdown();server.server_close();thread.join(2)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'model':'Local HTTP; synthetic extra metadata; no TLS or Yandex','results':rows},indent=2),encoding='utf-8')
    print(json.dumps(rows))


if __name__=='__main__':main()
