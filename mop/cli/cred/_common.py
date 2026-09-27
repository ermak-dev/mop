"""Общее командлетов `mop cred`: не подкоманда (имя с подчёркиванием)."""
import time

from mop.common import credreg
from mop.common.render import table


def print_creds(ans):
    """Ответ cred_list / cred_status -> таблица `mop cred list` в stdout."""
    print("\n".join(table(credreg.rows(ans.get("creds") or [], time.time(),
                                       ans.get("holders") or {}))))
