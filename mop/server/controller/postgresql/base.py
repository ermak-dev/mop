"""Тонкий backend: публичный psycopg 3.3.6 wait(timeout), без замены SQL.

PQfinish вызывается владельцем потока: соединение больше не используется.
Это не доказательство немедленного освобождения locks недоступным сервером.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time

import psycopg
from django.conf import settings
from django.db import DatabaseError
from django.db.backends.postgresql.base import DatabaseWrapper as PostgreSQLWrapper

_deadline = ContextVar('controller_transaction_deadline', default=None)


@contextmanager
def transaction_deadline(seconds):
    """Один абсолютный бюджет, включая последовательные короткие запросы."""
    previous = _deadline.get()
    expires = time.monotonic() + seconds
    token = _deadline.set(min(previous, expires) if previous is not None else expires)
    try:
        yield
    finally:
        _deadline.reset(token)


@contextmanager
def _cleanup_deadline():
    # ROLLBACK и восстановление autocommit имеют отдельный конечный transport
    # timeout. Истёкший бюджет тела не должен мешать освобождению соединения.
    token = _deadline.set(None)
    try:
        yield
    finally:
        _deadline.reset(token)


class TransportDeadlineExceeded(psycopg.OperationalError):
    """Ожидание протокола закончено, сокет fenced; retry здесь не выполняется."""


class CommitOutcomeUnknown(psycopg.OperationalError):
    """Потерянный ответ COMMIT нельзя объявлять rollback."""
    outcome_unknown = True


class BoundedConnection(psycopg.Connection):
    def wait(self, gen, interval=.1, timeout=None):
        limit = float(getattr(settings, 'CONTROLLER_DB_TIMEOUT', 5))
        if not math.isfinite(limit) or not 0 < limit <= 5:
            raise ValueError('database transport timeout must be within 5 seconds')
        expires = _deadline.get()
        if expires is not None:
            limit = min(limit, expires - time.monotonic())
        if timeout is not None:
            limit = min(limit, timeout)
        if limit > 0:
            try:
                return super().wait(gen, interval=interval, timeout=limit)
            except psycopg.errors._WaitTimeout:
                pass
        # Не отправлять cancel и не ждать его подтверждения по больной сети.
        self.pgconn.finish()
        raise TransportDeadlineExceeded('controller database transport deadline exceeded; connection fenced') from None

    def commit(self):
        try:
            return super().commit()
        except psycopg.OperationalError as exc:
            if exc.sqlstate is not None:
                raise
            self.pgconn.finish()
        raise CommitOutcomeUnknown('controller database commit outcome unknown; reconcile before retry') from None


class _Driver:
    # Только локальная facade backend, не monkeypatch общего модуля psycopg.
    def connect(self, *args, **kwargs):
        return BoundedConnection.connect(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(psycopg, name)


class DatabaseWrapper(PostgreSQLWrapper):
    Database = _Driver()

    def __init__(self, settings_dict, alias='default'):
        options = dict(settings_dict.get('OPTIONS', {}))
        pool = options.get('pool')
        if pool:
            # connection_class — поддержанный параметр psycopg_pool, не
            # несуществующий connection_factory параметр psycopg.connect.
            options['pool'] = {**({} if pool is True else pool), 'connection_class': BoundedConnection}
        super().__init__({**settings_dict, 'OPTIONS': options}, alias)

    def _rollback(self):
        try:
            with _cleanup_deadline():
                return super()._rollback()
        except DatabaseError:
            if self.connection is not None and self.connection.closed:
                # Atomic.__exit__ не должен заново подключаться при cleanup и
                # заменять исходный outcome unknown новым connect/pool timeout.
                self.close()
                self.closed_in_transaction = True
            raise

    def set_autocommit(self, autocommit, force_begin_transaction_with_broken_autocommit=False):
        if autocommit:
            with _cleanup_deadline():
                return super().set_autocommit(autocommit, force_begin_transaction_with_broken_autocommit)
        return super().set_autocommit(autocommit, force_begin_transaction_with_broken_autocommit)
