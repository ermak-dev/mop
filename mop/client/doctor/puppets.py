"""puppets: stuck sessions, a stale login, restart backoff, model quota, an old job spec

Сегодняшний doctor целиком (#356): диагноз и лечение живут в
mop/common/puppets.py, раздача кредов перед login+nudge -- в keys.
"""
from mop.client import keys
from mop.common import puppets


def diagnose():
    return puppets.diagnose()


def prepare(issues):
    """Раздача кредов перед лечением -- только если есть кого будить. Строки
    -- узлы, куда креды не доехали (#159): успех виден по лечению ниже."""
    if not any(i["action"] == "login+nudge" for i in issues):
        return None, []
    if not keys.credentials_fresh():
        return ("local credentials are stale or broken — log in to claude "
                "on this machine first, then mop doctor --fix"), []
    results, what, _ = keys.push_login()
    return None, [f"  {node}: {' + '.join(what)} {results[node]}"
                  for node in sorted(results) if results[node] != "OK"]


def treat(issue):
    return puppets.treat(issue)
