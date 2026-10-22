"""Keep expensive history/disk accounting off HTTP and control threads."""
import copy
import threading
import time
from collections import OrderedDict


class StatusCache:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.items = OrderedDict()
        self.pending = set()
        self.generation = 0

    def invalidate(self):
        with self.lock:
            self.items.clear()
            self.generation += 1

    def snapshot(self, job):
        key = job['id']
        with self.lock:
            token = (self.generation, key)
            item = self.items.get(key)
            result = copy.deepcopy(item['value']) if item and item.get('value') else self.empty(job)
            result['statistics_error'] = item.get('error', '') if item else ''
            due = item.get('due', 0) if item else 0
            if item and item.get('value') and item['value']['phase'] != job['phase']:
                due = 0
                # Old counters must not appear as a finished scan / download.
                # Controls remain immediate while the new phase is counted.
                result['statistics_pending'] = True
            if token not in self.pending and len(self.pending) < 2 and time.monotonic() >= due:
                self.pending.add(token)
                try:
                    threading.Thread(target=self.refresh, args=(key, token), name='disk-statistics', daemon=True).start()
                except RuntimeError:
                    self.pending.discard(token)
                    result['statistics_error'] = 'На ПК не удалось запустить подсчёт. Закройте лишние программы; приложение повторит попытку.'
                    self.items[key] = {'value':None,'due':time.monotonic()+5,'error':result['statistics_error']}
        result.update(job)
        control = self.engine.controls.get(key)
        thread = self.engine.threads.get(key)
        live = bool(thread and thread.is_alive())
        result.update(control.snapshot() if control else {'active': [], 'speed': 0, 'session':None})
        if not live:result['active'] = []
        result['live'] = live
        result['storage'] = control.storage_snapshot() if control else None
        result['requires_connection'] = not (live and control and not control.requires_connection())
        finishing = job['phase'] in ('ready', 'stopped', 'failed', 'completed', 'completed_with_errors')
        if live and control and not finishing:
            result['phase'] = 'stopping' if control.stopped else 'paused' if control.paused else job['phase']
        result['can_pause'] = bool(live and control and not finishing and not control.stopped and not control.paused)
        result['can_resume'] = bool(not live or control and control.paused and not control.stopped)
        result['network_waiting'] = bool(live and control and control.network.offline.is_set())
        result['network_warning'] = control.network.message if result['network_waiting'] else ''
        if not live or control and control.paused or result['network_waiting']:
            result['speed'] = 0
        if control:
            result['pause_reason'] = control.pause_reason or job.get('pause_reason', '')
        return result

    def refresh(self, key, token):
        try:
            with self.engine.cached_storage_status():
                value = self.engine.status(key)
            value['statistics_pending'] = False
            with self.lock:
                if token[0] != self.generation:
                    return
                self.items[key] = {'value': value, 'due': time.monotonic()+(3 if value.get('live') else 10)}
                self.items.move_to_end(key)
                while len(self.items) > 40:
                    self.items.popitem(last=False)
        except Exception as exc:
            with self.lock:
                if token[0] != self.generation:
                    return
                item = self.items.setdefault(key, {})
                item['due'] = time.monotonic()+5
                item['error'] = 'Приложение на ПК не смогло обновить счётчики. Управление заданием доступно. Если повторяется, отправьте диагностику.'
            recorder = getattr(self.engine, 'request_errors', None)
            if recorder:
                recorder.record('statistics', 'job_statistics', exc)
        finally:
            with self.lock:
                self.pending.discard(token)

    @staticmethod
    def empty(job):
        return dict(job, statistics_pending=True, counts={}, files=0, folders=0, processed=0,
                    percent=None, originals=0, previews=0, replacements=0, existing=0,
                    missing_previews=0, cancelled=0, errors=0, scan_errors=0,
                    saved_bytes=0, previous_results=0, partial_bytes=0, local_bytes=0,
                    folder_bytes=None, folder_bytes_scanning=True, free_bytes=None,
                    completeness={'selected':0,'originals':0,'preview_only':0,'no_result':0},
                    workers_status={'current':job['settings']['workers'],'maximum':job['settings']['workers'],'enabled':False,'reason':''},
                    estimate={key:0 for key in ('known_bytes','unknown_sizes','zip_basis_bytes','original_bytes','original_unknown','extra_temporary_bytes','conditional_preview_sizes')},
                    eta_seconds=None, eta_phase=None, eta_message='Получаем статистику…',
                    journal_saved=None, journal_error='')
