import os
import sys
from pathlib import Path


def frozen():
    return bool(getattr(sys, "frozen", False))


def state_folder():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'YaDiskDownloader'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'YaDiskDownloader'
    base = Path(os.environ.get('XDG_STATE_HOME', ''))
    if not base.is_absolute():
        base = Path.home() / '.local' / 'state'
    return base / 'YaDiskDownloader'


def program_folder():
    return Path(sys.executable).resolve().parent if frozen() else Path(__file__).resolve().parents[1]


def destination_folder():
    if frozen() or os.name != 'nt':
        return Path.home() / "Downloads" / "YaDiskDownloader"
    return Path(__file__).resolve().parents[1] / "downloads"


def external_environment():
    """System programs must not load shared libraries from the frozen bundle."""
    env = dict(os.environ)
    if frozen():
        for key in ('LD_LIBRARY_PATH','DYLD_LIBRARY_PATH'):
            original = env.get(key+'_ORIG')
            if original:env[key] = original
            else:env.pop(key,None)
    return env
