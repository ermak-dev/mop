"""LLM proxy: the pool's single model server must answer

Контроллер -- единственная точка всего LLM (#379): подписки, ключи и
ротация живут в прокси, и его тишина останавливает и папетов, и мастеров.
Диагноз -- один GET /v1/models клиентским ключом: не 200 -- проблема без
автолечения. Слова называют юнит на контроллере: рестарт папетов здесь
ничего не чинит (#383).
"""
import urllib.error
import urllib.request

from mop.common import config, llm

TIMEOUT = 10


def issues_of(answer):
    """Ответ /v1/models -> [{diagnosis, action}]: код, исключение или 200.
    Чистая: разбор без сети, сам запрос -- diagnose()."""
    if answer == 200:
        return []
    if isinstance(answer, Exception):
        why = f"{type(answer).__name__}: {answer}"
    else:
        why = f"HTTP {answer}"
    return [{"name": "mop-llm-proxy", "node": None, "alloc": None,
             "diagnosis": f"LLM proxy is down ({why}) — the unit mop-llm-proxy "
                          f"on the controller, mop server deploy brings it back",
             "action": None}]


def _probe():
    """Код ответа /v1_models | исключение сети."""
    req = urllib.request.Request(config.get("MOP_PROXY_URL").rstrip("/") + "/v1/models",
                                 headers={"Authorization": f"Bearer {config.get(llm.KEY)}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001 -- любая сеть читается причиной
        return e


def diagnose():
    """Проблемы группы: прокси недоступен или ключ не принимает."""
    try:
        code = _probe()
    except Exception as e:  # noqa: BLE001
        code = e
    return issues_of(code if code is not None else 500)


def prepare(issues):
    """Готовить нечего: лечение -- оператора."""
    return None, []


def treat(issue):
    """Автолечения нет: юнит на контроллере, не папет."""
    return "no auto-treatment — operator's call"
