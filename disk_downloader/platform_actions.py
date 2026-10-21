"""Open local results with argument lists, never shell interpolation."""
import os
from pathlib import Path
import subprocess
import sys
from .runtime import external_environment


def open_browser(url):
    if os.name=='nt':
        import webbrowser
        return webbrowser.open(url)
    command=['/usr/bin/open',url] if sys.platform=='darwin' else ['xdg-open',url]
    try:subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,env=external_environment())
    except FileNotFoundError:raise ValueError('Браузер не удалось открыть автоматически. Откройте адрес '+url+' вручную.') from None
    return True


def open_path(path, folder=False):
    path=str(Path(path).absolute())
    if os.name=='nt':
        os.startfile(path,'explore' if folder else 'open')
    else:
        command=['/usr/bin/open',path] if sys.platform=='darwin' else ['xdg-open',path]
        try:subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,env=external_environment())
        except FileNotFoundError:raise ValueError('Системный просмотрщик недоступен. Откройте указанный путь вручную.') from None
