"""Optional local scheduling and quiet hours; never stores account credentials."""
from datetime import datetime
import time
import threading


def validate(data):
    run_at=data.get('run_at','')
    if not isinstance(run_at,str) or len(run_at)>40:raise ValueError('Некорректное время запуска')
    if run_at:
        try:
            stamp=datetime.fromisoformat(run_at.replace('Z','+00:00'))
            if stamp.tzinfo is None:raise ValueError()
        except ValueError:raise ValueError('Укажите дату и время запуска с часовым поясом') from None
    quiet=data.get('quiet_hours',{})
    if not isinstance(quiet,dict) or set(quiet)-{'enabled','start','end','speed_limit_mib','mute'}:raise ValueError('Некорректные тихие часы')
    enabled, mute=quiet.get('enabled',False),quiet.get('mute',True)
    if not isinstance(enabled,bool) or not isinstance(mute,bool):raise ValueError('Некорректная настройка тихих часов')
    hours={}
    for key,default in [('start',23),('end',8)]:
        value=quiet.get(key,default)
        if type(value) is not int or not 0<=value<=23:raise ValueError('Час начала и окончания: от 0 до 23')
        hours[key]=value
    from .rate_limit import validate as speed
    return {'run_at':run_at,'quiet_hours':dict(enabled=enabled,mute=mute,speed_limit_mib=speed(quiet.get('speed_limit_mib',0)),**hours)}


def due(settings, now=None):
    value=settings.get('run_at')
    return not value or (now or datetime.now().astimezone()) >= datetime.fromisoformat(value.replace('Z','+00:00'))


def quiet(settings, hour=None):
    value=settings.get('quiet_hours',{})
    if not value.get('enabled'):return False
    hour=datetime.now().hour if hour is None else hour
    start,end=value.get('start',23),value.get('end',8)
    return start<=hour<end if start<end else hour>=start or hour<end


class Policy:
    def __init__(self, settings, control):
        self.settings=settings;self.control=control;self.next=0;self.last=None;self.last_rate=None;self.lock=threading.Lock()

    def update_speed(self, mib):
        with self.lock:
            self.settings['speed_limit_mib']=mib
            self._check(time.monotonic())

    def check(self):
        now=time.monotonic()
        if now<self.next:return
        if not self.lock.acquire(blocking=False):return
        try:
            self._check(now)
        finally:self.lock.release()

    def _check(self,now):
        self.next=now+10
        active=quiet(self.settings)
        rate=self.settings.get('quiet_hours',{}).get('speed_limit_mib',0) if active else self.settings.get('speed_limit_mib',0)
        if active!=self.last or rate!=self.last_rate:
            self.control.rate_limiter.configure(rate)
            self.last,self.last_rate=active,rate
