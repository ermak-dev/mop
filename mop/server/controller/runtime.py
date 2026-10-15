"""Composition root и граница синхронного Django с async I/O."""
import os
from asgiref.sync import sync_to_async


def configure():
    """Явное подключение framework, не побочный эффект common/config."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mop.server.controller.settings')
    import django
    django.setup()


async def database_call(function, *args, **kwargs):
    """Целая sync-функция на thread-sensitive границе."""
    return await sync_to_async(function, thread_sensitive=True)(*args, **kwargs)
