"""Полный trusted proof собственного Unix fixture, не native readiness evidence."""
import os
from pathlib import Path

from mop import session
from mop.client.local_bridge import OwnSession


def own_session(path, conversation):
    """Роль launcher в тесте: процесс/namespace/cwd измерены, ID назначен fixture."""
    pid = os.getpid()
    snapshot = session.native_process(pid)
    return OwnSession(str(path), pid, conversation, process_start=snapshot['start'],
        uid=snapshot['uid'], namespaces=snapshot['namespaces'], cwd=Path(snapshot['cwd']))
