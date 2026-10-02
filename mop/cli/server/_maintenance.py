"""Проверка запуска обслуживания только на контроллере."""
import os

from mop.server import natsconf


def require():
    """Отказ до шины, если нет локального файла учёток сервиса сервера."""
    if not os.path.isfile(natsconf.BASE) or not os.access(natsconf.BASE, os.R_OK):
        raise RuntimeError("server maintenance requires the controller's local service credentials")


def only_server(fn):
    """Оборачивает команду проверкой контроллера до чтения пула."""
    def wrap(argv):
        require()
        return fn(argv)
    return wrap
