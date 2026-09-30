"""puppets: stuck sessions, a stale login, restart backoff, model quota, an old job spec

Сегодняшний doctor целиком (#356): диагноз и лечение живут в
mop/common/puppets.py. Протухший логин (#357) лечится арендой папета из
реестра кредитов на сервере: cred_push (#360) отдаёт её в тело, потом
побудка. Локального файла машины запуска doctor не читает -- расписанный с
сервера doctor (#359) его и не имеет. Папет без аренды не лечится:
назначить кредит -- дело `mop update --cred`, а не доктора.
"""
from mop.common import bus, puppets, state

NO_LEASE = "no lease — mop update --cred"


def diagnose():
    """Проблемы puppets.diagnose; login+nudge без аренды -- без лечения."""
    out = []
    for issue in puppets.diagnose():
        if issue["action"] == state.LOGIN_NUDGE and not issue.get("lease"):
            issue = dict(issue, action=None, diagnosis=f"{issue['diagnosis']}; {NO_LEASE}")
        out.append(issue)
    return out


def prepare(issues):
    """Готовить нечего: аренда раздаётся каждому папету в treat, отказ
    одного не останавливает лечение остальных."""
    return None, []


def push_outcome(name, reply):
    """Ответ cred_push -> None (аренда в теле) либо почему нет. Чистая.
    Глагол отказом не отвечает: ok=True с result/refused, их и разбираем."""
    if not reply.get("lease"):
        return f"lease not pushed: {name} holds no lease any more — mop update --cred"
    if reply.get("refused"):
        return f"lease not pushed: {reply['refused']}"
    if reply.get("result") != "OK":
        return f"lease not pushed: {reply.get('result') or 'no result'}"
    return None


def treat(issue):
    """login+nudge: аренду -- в тело, потом побудка (#290, без рестарта);
    не дошла -- причина вместо побудки. Остальное -- puppets.treat."""
    if issue["action"] == state.LOGIN_NUDGE:
        # Из контекста вызывающего: мастер проекта -- своему папету (#360),
        # оператор -- любому.
        try:
            why = push_outcome(issue["name"], bus.call_cluster("cred_push", name=issue["name"]))
        except Exception as e:   # как puppets.treat: отказ строкой, не трассой
            why = f"lease not pushed: {e}"
        if why:
            return why
    return puppets.treat(issue)
