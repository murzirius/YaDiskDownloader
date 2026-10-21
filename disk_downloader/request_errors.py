"""Bounded technical records without tokens, paths, request bodies or locals."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import traceback
import sqlite3

from .sqlite_support import error_details


class RequestErrors:
    def __init__(self, state):
        self.lock = threading.Lock()
        self.items = deque(maxlen=30)
        self.logger = logging.Logger('application-errors')
        try:
            handler = RotatingFileHandler(Path(state)/'application-errors.jsonl', maxBytes=256*1024, backupCount=2, encoding='utf-8', delay=True)
            handler.setFormatter(logging.Formatter('%(message)s'))
            self.logger.addHandler(handler)
        except OSError:
            pass

    def record(self, request_id, endpoint, exc, *, phase=None):
        value = {'request_id':request_id, 'time':datetime.now(timezone.utc).isoformat(),
                 'operation':endpoint, 'exception':type(exc).__name__,
                 'sqlite_code':getattr(exc, 'sqlite_errorname', ''),
                 'frames':[{'file':Path(frame.filename).name, 'function':frame.name, 'line':frame.lineno}
                           for frame in traceback.extract_tb(exc.__traceback__)[-12:]]}
        if isinstance(exc, sqlite3.Error):
            value['sqlite'] = error_details(exc)
        if phase:
            value['phase'] = phase
        with self.lock:
            self.items.append(value)
            if self.logger.handlers:
                try:
                    self.logger.error(json.dumps(value, ensure_ascii=False))
                except OSError:
                    pass
                finally:
                    for handler in self.logger.handlers:
                        handler.close()

    def snapshot(self):
        with self.lock:
            return list(self.items)
