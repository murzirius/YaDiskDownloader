"""Bounded task queries and non-blocking destination checks."""
import json
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from .import_state import find_import


def download_plan(store, job_id):
    job = store.job(job_id)
    mode = 'both' if job['settings'].get('missing_policy') == 'both' else 'fallback' if job['settings'].get('operation') == 'missing' or job['generation'] else job['settings']['mode']
    with store.db() as db:
        row = db.execute("""WITH copies AS (
          SELECT e.path,e.meta,e.status,e.original_status,e.preview_status,
          EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='original') AS original,
          EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='preview') AS preview
          FROM entries e WHERE e.job=? AND e.kind='file' AND e.status!='filtered')
          SELECT COUNT(*) AS files,COALESCE(SUM(original),0) AS originals,
          COALESCE(SUM(preview AND NOT original),0) AS preview_only,
          COALESCE(SUM(NOT original AND NOT preview),0) AS missing,
          COALESCE(SUM(status='pending'),0) AS to_download,
          COALESCE(SUM(original_status='downloaded_verified'),0) AS new_originals,
          COALESCE(SUM(preview_status='downloaded_verified'),0) AS new_previews,
          COALESCE(SUM(status='existing'),0) AS skipped,
          COALESCE(SUM(CASE WHEN status='pending' AND NOT original AND (?!='previews' OR COALESCE(json_extract(meta,'$.file_type'),'files') NOT IN ('photos','videos')) THEN COALESCE(json_extract(meta,'$.size'),0) ELSE 0 END),0) AS known_bytes,
          COALESCE(SUM(status='pending' AND json_extract(meta,'$.has_preview')=1),0) AS possible_previews
          FROM copies""", (job_id,mode)).fetchone()
    return dict(row) | {'ready':job['phase']=='ready', 'root':job['root'], 'policy':job['settings'].get('missing_policy','originals'),
                        'warning':job['settings'].get('destination_warning',''),
                        'note':'Проверяются скачанные файлы. Журналы и отчёты не входят в проверку содержимого. Размер превью заранее неизвестен.'}


def files(store, job_id, query='', state='all', offset=0):
    store.job(job_id)
    if not isinstance(query,str) or len(query)>200 or state not in ('all','original','preview','missing','problems'):
        raise ValueError('Укажите имя файла и допустимый фильтр результатов.')
    if not isinstance(offset,int) or not 0<=offset<=10000000:
        raise ValueError('Некорректная страница списка файлов.')
    condition = {
        'all':'1', 'original':"EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='original')",
        'preview':"EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='preview')",
        'missing':"NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.job=e.job AND a.path=e.path)",
        'problems':"(e.error!='' OR e.status IN ('error','no_preview'))"}[state]
    pattern='%'+query.casefold().replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    with store.db() as db:
        rows=db.execute("""SELECT e.path,e.status,e.error,e.original_status,e.preview_status,
          (SELECT local FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='original') AS original,
          (SELECT local FROM artifacts a WHERE a.job=e.job AND a.path=e.path AND a.type='preview') AS preview
          FROM entries e WHERE e.job=? AND e.kind='file' AND e.status!='filtered'
          AND unicode_fold(e.path) LIKE ? ESCAPE '!' AND """+condition+' ORDER BY e.path LIMIT 101 OFFSET ?', (job_id,pattern,offset)).fetchall()
    return {'items':[dict(row) for row in rows[:100]],'has_more':len(rows)>100,'offset':offset}


def problems(store, job_id):
    store.job(job_id)
    with store.db() as db:
        rows=db.execute("""SELECT error,COUNT(*) AS files FROM (
          SELECT error FROM entries WHERE job=? AND kind='file' AND error!=''
          UNION ALL SELECT error FROM scan_errors WHERE job=?) GROUP BY error ORDER BY files DESC,error LIMIT 50""", (job_id,job_id)).fetchall()
    groups=[]
    for row in rows:
        text=row['error'];lower=text.casefold()
        if any(word in lower for word in ('ключ','токен','401','подключите диск')):
            source,action='Подключение Яндекс.Диска','connect'
        elif any(word in lower for word in ('интернет','соединен','сетевая','dns','таймаут')):
            source,action='Интернет или ответ Яндекс.Диска','retry'
        elif any(word in lower for word in ('папк','файл на пк','windows','места','накопител','файловой системы')):
            source,action='Файлы и накопитель на устройстве','folder'
        elif any(word in lower for word in ('яндекс','api','оригинал:','превью:')):
            source,action='Яндекс.Диск','retry'
        else:
            source,action='Приложение на устройстве','diagnostics'
        groups.append(dict(row) | {'source':source,'action':action})
    return {'groups':groups}


def inspect_destination(store, value):
    if not isinstance(value,str) or not Path(value).is_absolute() or len(value)>30000 or '\0' in value:
        raise ValueError('Укажите полный путь к папке на устройстве.')
    path=Path(value).resolve()
    if path.exists() and not path.is_dir():
        raise ValueError('По этому пути находится файл. Выберите папку.')
    parent=path
    while not parent.exists() and parent!=parent.parent:
        parent=parent.parent
    if not parent.is_dir():
        raise ValueError('Накопитель недоступен. Подключите его и выберите папку снова.')
    with tempfile.TemporaryFile(dir=parent,prefix='.yadisk-write-test-') as probe:
        probe.write(b'check');probe.flush()
    usage=shutil.disk_usage(parent)
    previous=find_import(store,path) if path.is_dir() else None
    return {'path':str(path),'exists':path.is_dir(),'writable':True,'free_bytes':usage.free,'total_bytes':usage.total,
            'used_percent':round((usage.total-usage.free)*100/max(1,usage.total),1),'previous_import':bool(previous),
            'previous_paths':previous['job']['settings'].get('paths',[]) if previous else []}


class DestinationChecks:
    def __init__(self, store):
        self.store=store;self.lock=threading.Lock();self.value={'id':None,'busy':False}

    def snapshot(self):
        with self.lock:return dict(self.value)

    def start(self, path):
        if not isinstance(path,str) or not path or len(path)>30000:
            raise ValueError('Введите полный путь к папке на устройстве.')
        with self.lock:
            if self.value['busy']:
                if self.value.get('path') == path:return dict(self.value)
                raise ValueError('Предыдущая папка ещё отвечает. Подождите или проверьте подключение сетевого накопителя.')
            identity=uuid.uuid4().hex
            self.value={'id':identity,'busy':True,'result':None,'error':'','path':path}
            worker=threading.Thread(target=self.run,args=(identity,path),daemon=True,name='disk-destination-check')
            try:worker.start()
            except RuntimeError:
                self.value['busy']=False
                raise ValueError('Приложение на устройстве не смогло начать проверку папки. Повторите.')
            return dict(self.value)

    def run(self, identity, path):
        result=None;error=''
        try:result=inspect_destination(self.store,path)
        except (OSError,ValueError):error='Папка на устройстве недоступна для записи. Проверьте путь, права доступа и подключение накопителя.'
        except Exception:error='Приложение на устройстве не смогло проверить папку. Повторите проверку или соберите диагностику.'
        finally:
            with self.lock:self.value={'id':identity,'busy':False,'result':result,'error':error}
