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
_cleaning = ContextVar('controller_transaction_cleanup', default=False)


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
    cleaning = _cleaning.set(True)
    try:
        yield
    finally:
        _cleaning.reset(cleaning)
        _deadline.reset(token)


class TransportDeadlineExceeded(psycopg.OperationalError):
    """Ожидание протокола закончено, сокет fenced; retry здесь не выполняется."""


class CommitOutcomeUnknown(psycopg.OperationalError):
    """Потерянный ответ COMMIT нельзя объявлять rollback."""
    outcome_unknown = True


class BoundedConnection(psycopg.Connection):
    _transaction_expires = None

    def wait(self, gen, interval=.1, timeout=None):
        limit = float(getattr(settings, 'CONTROLLER_DB_TIMEOUT', 5))
        if not math.isfinite(limit) or not 0 < limit <= 5:
            raise ValueError('database transport timeout must be within 5 seconds')
        now = time.monotonic()
        expires = _deadline.get()
        if expires is not None:
            limit = min(limit, expires - now)
        if not _cleaning.get() and not self.autocommit:
            if self._transaction_expires is None:
                budget = float(getattr(settings, 'CONTROLLER_TRANSACTION_TIMEOUT', 10))
                if not math.isfinite(budget) or not 0 < budget <= 10:
                    raise ValueError('database transaction timeout must be within 10 seconds')
                self._transaction_expires = now + budget
            limit = min(limit, self._transaction_expires - now)
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
            result = super().commit()
        except psycopg.OperationalError as exc:
            if exc.sqlstate is not None:
                raise
            self.pgconn.finish()
        else:
            self._transaction_expires = None
            return result
        raise CommitOutcomeUnknown('controller database commit outcome unknown; reconcile before retry') from None

    def rollback(self):
        with _cleanup_deadline():
            result = super().rollback()
        self._transaction_expires = None
        return result


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

    def _set_autocommit(self, autocommit):
        if autocommit:
            # Только driver step: on_commit callbacks Django запускает позднее,
            # уже вне cleanup scope, с обычным transaction deadline.
            with _cleanup_deadline():
                result = super()._set_autocommit(autocommit)
            self.connection._transaction_expires = None
            return result
        return super()._set_autocommit(autocommit)

    def close_pool(self):
        pool = self._connection_pools.get(self.alias)
        if pool is None:
            return
        # В закреплённом psycopg_pool 3.3.6 close() очищает эти ссылки и лишь
        # предупреждает об истёкшем join. Сохраняем реальных владельцев до stop.
        workers = getattr(pool, '_controller_closing_workers', None)
        if workers is None:
            workers = tuple(pool._workers)
            if pool._sched_runner is not None:
                workers += (pool._sched_runner,)
            pool._controller_closing_workers = workers
        pool.close(timeout=10)
        if any(worker.is_alive() for worker in workers):
            raise RuntimeError('controller database pool shutdown incomplete: driver threads are still running')
        self._connection_pools.pop(self.alias, None)
