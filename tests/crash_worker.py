"""Isolated child used by test_process_crash; never touches a user's account."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from conftest import FakeAPI, Response, wait_job
from disk_downloader.engine import Engine
from disk_downloader.store import Store

folder=Path(sys.argv[1]);phase=sys.argv[2]
api=FakeAPI();api.file('disk:/one.bin',data=b'x'*(8*1024*1024),preview=False)
store=Store(folder/'state/jobs.sqlite3');engine=Engine(store,api)
base_media=api.media
def fast_media(*args,**kwargs):
    response,session=base_media(*args,**kwargs)
    response.chunks=1024*1024
    return response,session
api.media=fast_media

def stop_here():
    (folder/'reached').write_text('ready',encoding='ascii')
    while True: time.sleep(.1)

if phase=='scan':
    for n in range(450):api.file('disk:/item-'+str(n)+'.bin',data=b'item',preview=False)
    original=api.metadata
    def metadata(*args,**kwargs):
        if kwargs.get('offset',0)>=200:stop_here()
        return original(*args,**kwargs)
    api.metadata=metadata
elif phase=='download':
    original=api.media
    def media(path,kind,settings,checkpoint=lambda:None):
        response,session=original(path,kind,settings,checkpoint)
        class Blocks(Response):
            def iter_content(self,chunk_size):
                for start in range(0,len(self.data),1024*1024):
                    if start>=5*1024*1024:stop_here()
                    yield self.data[start:start+1024*1024]
        return Blocks(response.data),session
    api.media=media
elif phase=='publish':
    import disk_downloader.engine as module
    original=module.publish
    def publish(source,target):
        original(source,target)
        if str(target).endswith('one.bin'):stop_here()
    module.publish=publish
elif phase=='reports':
    import disk_downloader.reports as module
    original=module.finish
    def finish(*args,**kwargs):
        if args[2]=='report:json':stop_here()
        return original(*args,**kwargs)
    module.finish=finish
elif phase=='zip':
    import zipfile
    original=zipfile.ZipFile.open
    def opened(self,name,mode='r',*args,**kwargs):
        stream=original(self,name,mode,*args,**kwargs)
        if mode=='w' and getattr(name,'filename',name)=='one.bin':
            original_write=stream.write
            def write(data):
                count=original_write(data);stop_here();return count
            stream.write=write
        return stream
    zipfile.ZipFile.open=opened

job_id=engine.create({'destination':str(folder/'results'),'whole_disk':True,'mode':'fallback','workers':1,'attempts':1,'timeout':60,'reports':['json'],'auto_pause':False})
(folder/'job').write_text(job_id,encoding='ascii')
if phase!='scan':
    wait_job(engine,job_id);engine.action(job_id,'start')
while True:time.sleep(.1)
