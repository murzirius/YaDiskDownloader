"""FIFO admission for SQLite writers; WAL readers remain independent."""
from collections import deque
import os
import sqlite3
import threading
import weakref


class WriteGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.waiting = deque()
        self.owner = None
        self.thread = None

    def acquire(self, owner, checkpoint=None):
        with self.condition:
            if self.thread == threading.get_ident():
                raise sqlite3.OperationalError('Nested write connections are not supported')
            self.waiting.append(owner)
            try:
                if checkpoint is not None:
                    checkpoint()
                while self.owner is not None or self.waiting[0] is not owner:
                    self.condition.wait(timeout=.05 if checkpoint is not None else None)
                    if checkpoint is not None:
                        checkpoint()
                self.waiting.popleft()
                self.owner, self.thread = owner, threading.get_ident()
            except BaseException:
                if owner in self.waiting:
                    self.waiting.remove(owner)
                if self.owner is owner:
                    self.owner = self.thread = None
                self.condition.notify_all()
                raise

    def release(self, owner):
        with self.condition:
            if self.owner is owner:
                self.owner = self.thread = None
                self.condition.notify_all()


_gates = weakref.WeakValueDictionary()
_lock = threading.Lock()


def gate_for(filename):
    key = os.path.normcase(os.path.realpath(filename))
    with _lock:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates[key] = WriteGate()
        return gate


class WriteConnection(sqlite3.Connection):
    gate = None
    admitted = False
    admission_checkpoint = None

    def admit(self, sql):
        command = sql.lstrip().split(None,1)[0].upper() if sql.strip() else ''
        # PRAGMAs used by Store.db() configure the connection, not shared data.
        # All application DML/DDL (including WITH or explicit BEGIN) is admitted.
        if self.gate and not self.admitted and command not in ('', 'SELECT', 'EXPLAIN', 'PRAGMA'):
            self.gate.acquire(self, self.admission_checkpoint)
            self.admitted = True

    def execute(self, sql, parameters=(), /):
        self.admit(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        self.admit(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql, /):
        self.admit(sql)
        return super().executescript(sql)

    def cursor(self, factory=None):
        return super().cursor(factory or WriteCursor)

    def close(self):
        try:
            super().close()
        finally:
            self.release_admission()

    def release_admission(self):
        if self.admitted:
            self.admitted = False
            self.gate.release(self)


class WriteCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        self.connection.admit(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        self.connection.admit(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql, /):
        self.connection.admit(sql)
        return super().executescript(sql)
