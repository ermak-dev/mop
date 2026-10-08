"""Серверный адаптер ротации клиентского ключа (#539).

Библиотека #538 -- ОДИН оркестратор: этот модуль только даёт ей
настоящие эффекты и не повторяет ни одного её решения. Транспорт --
management API петли установленного LLM-прокси (пин v8.0.16, #537),
probe -- клиентский GET /v1/models. Граница контроллера -- до сети и
файлов: инвентарь установки (как mop server user) плюс явный отказ
puppet-контексту (MOP_PROJECT/MOP_BUS_CONFIG сеет spec.py; UID границей
не является -- папет может жить под тем же UID и в том же доме).

Только stdlib; ключ management читается из файла при каждом эффекте и
никуда не выезжает: ни в argv, ни в env, ни в итоги.
"""
import json
import os
import urllib.error
import urllib.request

from ..common import bus, config, paths, puppets

# Пиннутые маршруты management API v8 (#537: config_v8.go:64-141):
# запись секции адресная, тело -- список ЦЕЛИКОМ; условной записи нет,
# отсюда обязательный exclusive-writer у оркестратора.
KEYS_ROUTE = "/v8/management/config/access/api-keys"
MODELS_ROUTE = "/v1/models"
# Подтверждённый отказ ДО применения -- только 401/403 management:
# прочие коды исход неясен (unknown-путь библиотеки), не «refused».
REFUSED_CODES = (401, 403)
# Только петля: management API установленного прокси отвечает с 127.0.0.1,
# никакой URI от вызывающего не принимается (внешний прокси не должен
# видеть Bearer даже для надёжного localhost-адреса -- ProxyHandler({})).
LOOPBACK = "127.0.0.1"


def port_refusal():
    """Непригодный порт настройки -> строка-отказ | None. Порт -- не
    клиентские данные: 1..65535, и точка."""
    raw = config.get("MOP_LLM_PROXY_PORT") or ""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return f"MOP_LLM_PROXY_PORT is not a number: {raw!r}"
    if not 1 <= port <= 65535:
        return f"MOP_LLM_PROXY_PORT is out of range: {raw!r}"
    return None


def base_url():
    """База management API: только петля, порт из настройки."""
    return f"http://{LOOPBACK}:{config.get('MOP_LLM_PROXY_PORT')}"


def refusal_if_not_controller(inventory=None):
    """Граница контроллера ДО сети и файлов. -> строка-отказ | None.

    Инвентарь -- признак машины установки (как mop server user: у
    оператора после join его нет). Puppet-контекст (MOP_PROJECT или
    MOP_BUS_CONFIG в окружении -- их сеет врапер spec.py) отказывает и на
    самой машине контроллера: папет живёт под тем же UID и в том же
    доме, окружение -- единственное, что их разделяет."""
    if os.environ.get("MOP_PROJECT") or os.environ.get("MOP_BUS_CONFIG"):
        return ("this is a puppet context (MOP_PROJECT/MOP_BUS_CONFIG): "
                "the proxy key rotation is a controller operation")
    path = inventory if inventory is not None else os.environ.get(
        "INVENTORY", "")
    if not os.path.isfile(path):
        return (f"the rotation runs on the controller, where the "
                f"installation lives (no {path or 'inventory.yaml'} here)")
    return None


def resolve_paths(cluster_copy=None):
    """(источник, копия кластера). Источник -- secrets контроллера
    (paths.local, дом текущего пользователя-сервиса), копия -- под
    MOP_HOME из настройки (.env, не окружение: значение обычно не
    экспортировано). Один и тот же физический путь разрешён -- режим и
    владельца до эффекта проверит библиотека (#538 _serviceable),
    ROOT+чужой-uid там же -- именованный блокер, не обход.
    -> (source, copy)."""
    src = paths.local(paths.SECRETS, "llm-proxy-client.pass")
    if cluster_copy is not None:
        return src, cluster_copy
    return src, paths.under(config.get("MOP_HOME") or "",
                            paths.SECRETS, "llm-proxy-client.pass")


def mgmt_key_path():
    """Закрытая копия management-ключа контроллера (#418): каталог
    текущего пользователя, содержимое читается при каждом эффекте и не
    выезжает наружу."""
    return paths.local(paths.SECRETS, "llm-proxy-mgmt.pass")


def all_nodes():
    """Полный известный состав узлов, БЕЗ ready-фильтра: offline-узел --
    тоже получатель, его NOT REACHED держит операцию открытой (#538).
    -> [имя] | исключение (недоступный состав -- блокер, не пустой
    набор: пустота молча «завершила» бы раздачу)."""
    return sorted({n.name for n in puppets.pool()})


def nodes_refusal(pool=None):
    """Недоступный состав узлов -> строка-блокер | None."""
    try:
        (pool if pool is not None else all_nodes)()
    except Exception as e:  # noqa: BLE001 -- причина решается оператором
        return f"cannot list the pool nodes ({type(e).__name__})"
    return None


class Effects:
    """Transport-швы оркестратора #538: fetch_list/put_list/probe/deliver.

    transport -- объект с get_keys/put_keys/models (граница urllib; живой
    транспорт -- `_Urllib` ниже, проверки подставляют свой). Ключ
    management читается из файла ПРИ КАЖДОМ эффекте: файл могла сменить
    роль web между шагами."""

    def __init__(self, transport=None, mgmt_key_path=None, port=None):
        self._transport = transport if transport is not None else _Urllib()
        self._mgmt = mgmt_key_path if mgmt_key_path is not None \
            else mgmt_key_path()
        self._port = port

    def _bearer(self):
        with open(self._mgmt, encoding="utf-8") as f:
            return f.read().rstrip("\r\n")

    def fetch_list(self):
        keys = self._transport.get_keys(self._bearer())
        if not isinstance(keys, list):
            raise RuntimeError("the key list answer is not a list")
        return keys

    def put_list(self, keys):
        why = self._transport.put_keys(self._bearer(), list(keys))
        if why is None:
            return ("applied", None)
        # Подтверждённый отказ ДО применения: только 401/403 handler'а
        # management (пин #537); прочее -- исключение, исход неясен.
        code = why.get("http_status")
        if code in REFUSED_CODES:
            return ("refused", {"http_status": code,
                                "reason": why.get("reason")})
        raise RuntimeError(f"unclear PUT outcome (HTTP {code})")

    def probe(self, key):
        return self._transport.models(key) == 200

    def deliver(self, key, nodes):
        # Прямой write-канал по ЯВНОМУ сохранённому списку (#539): старый
        # push() решает по sha-гейту и settled -- резюмируемая раздача с
        # FAILED/NOT REACHED «завершилась» бы или молча пропустила. Итоги
        # сводит bus.results_from, строгую оценку -- strict_settled #538.
        content = f"MOP_PROXY_KEY={key}\n"
        answers = bus.request_many(
            "write", list(nodes), timeout=bus.WRITE_TIMEOUT,
            files=[bus.as_file(paths.NODE_SECRETS, content)])
        return bus.results_from(list(nodes), answers)


class _Urllib:
    """Живой транспорт: urllib, только петля, без proxy и redirect.
    Ответ 401/403 management несёт http_status; redirect запрещён --
    Bearer management-ключа не должен уезжать никуда, кроме петли."""

    def _call(self, route, method, bearer, body=None):
        req = urllib.request.Request(
            base_url() + route, data=body, method=method,
            headers={"Authorization": f"Bearer {bearer}",
                     "Content-Type": "application/json"})
        # ProxyHandler({}) -- внешний HTTP_PROXY не видит даже localhost
        # с Bearer; HTTPRedirectHandler убран совсем.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get_keys(self, bearer):
        status, raw = self._call(KEYS_ROUTE, "GET", bearer)
        if status != 200:
            raise RuntimeError(f"GET api-keys answered HTTP {status}")
        return json.loads(raw)

    def put_keys(self, bearer, keys):
        status, raw = self._call(KEYS_ROUTE, "PUT", bearer,
                                 json.dumps(keys).encode())
        if status == 200:
            return None
        return {"http_status": status}

    def models(self, key):
        status, _ = self._call(MODELS_ROUTE, "GET", key)
        return status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправления запрещены: тихий уход с петли -- потеря контроля
    над тем, куда уехал Bearer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
