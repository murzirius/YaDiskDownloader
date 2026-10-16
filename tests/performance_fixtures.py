import json
from disk_downloader.engine import Engine
from disk_downloader.api import Credentials,DiskAPI

def seed_summary(store,root,mode,operation,generation,empty=False):
    engine=Engine(store,DiskAPI(Credentials()))
    settings=engine.validate_settings({'destination':str(root),'whole_disk':True,'mode':mode,'operation':operation,'auto_pause':False})
    key='summary-contract'
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,settings,root,service,phase,started,scan_complete,generation) VALUES(?,?,?,?,?,?,?,?)',(key,json.dumps(settings),str(root),str(root),'ready','2026-01-01',1,generation))
        if not empty:
            statuses=['pending','running','done','existing','partial','error','no_preview','cancelled','filtered']
            outcomes=['failed','downloaded_verified','existing_verified','not_requested','unavailable']
            for i in range(81):
                path=f'disk:/{i:03}.bin';kind='dir' if i%11==0 else 'file'
                meta={'path':path,'name':f'{i:03}.bin','type':kind,'size':None if i%4==0 else i*97,'file_type':['photos','videos','music','files'][i%4],'has_preview':i%3==0}
                db.execute('INSERT INTO entries(job,path,kind,meta,status,original_status,preview_status,error,fallback,priority) VALUES(?,?,?,?,?,?,?,?,?,?)',(key,path,kind,json.dumps(meta),statuses[i%9],outcomes[i%5],outcomes[(i+1)%5],'problem' if i%7==0 else '',int(i%3==0),i%3))
                for result_type in ['original','preview']:
                    if i%(2 if result_type=='original' else 3)==0:
                        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',(key,path,result_type,path[6:]+result_type,i+1,'fake-sha','fingerprint','M'))
            db.execute('INSERT INTO scan_errors VALUES(?,?,?)',(key,'disk:/failed-folder','failed'))
    return key
