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
from . import proxykey

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


def resolve_refusal(cluster_home=None):
    """Неабсолютный MOP_HOME -> строка-отказ | None. Пустая настройка
    иначе давала путь от CWD: молчаливая запись не туда. Проверка ДО
    эффектов, вместе с остальной границей."""
    home = cluster_home if cluster_home is not None \
        else (config.get("MOP_HOME") or "")
    if home and not os.path.isabs(home):
        return f"MOP_HOME must be absolute, got {home!r}"
    if not home:
        return "MOP_HOME is not set: the cluster service copy has no home"
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
    home = config.get("MOP_HOME") or ""
    # Абсолютность проверяет resolve_refusal; сюда попадает уже
    # прошедшее границу (start зовёт отказ до begin).
    return src, paths.under(home, paths.SECRETS, "llm-proxy-client.pass")


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


def all_nodes_guarded():
    """ОДИН защищённый вызов состава для --all-registered: два отдельных
    вызова -- это два отказа и два разных ответа, второй без защиты
    давал traceback. -> [имя] | строка-блокер."""
    try:
        return all_nodes()
    except Exception as e:  # noqa: BLE001 -- причина решается оператором
        return f"cannot list the pool nodes ({type(e).__name__})"


# Чистый токен субъекта шины (busnames.node вставляет имя как есть):
# точка режет токен, * и > -- маски, пробел/пустое -- не имя.
CLEAN_NODE = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def node_refusals(names, pool=None):
    """Явный список --nodes перед begin: каждое имя -- чистый токен
    субъекта И известный узел. Опечатка иначе замораживает получателя,
    который никогда не ответит (навечно pending), а точка или маска
    уезжает прямо в субъект шины. -> строка-отказ | None."""
    for name in names:
        if not name or set(name) - CLEAN_NODE:
            return (f"node name {name!r} is not a clean bus subject "
                    f"token (letters, digits, - and _)")
    try:
        known = set(pool if pool is not None else all_nodes())
    except Exception as e:  # noqa: BLE001 -- состав недоступен -- блокер
        return f"cannot list the pool nodes ({type(e).__name__})"
    unknown = [n for n in names if n not in known]
    if unknown:
        return ("unknown node names (not in the pool): "
                + ", ".join(unknown))
    return None


class Effects:
    """Transport-швы оркестратора #538: fetch_list/put_list/probe/deliver.

    transport -- объект с get_keys/put_keys/models (граница urllib; живой
    транспорт -- `_Urllib` ниже, проверки подставляют свой). Ключ
    management читается из файла ПРИ КАЖДОМ эффекте: файл могла сменить
    роль web между шагами."""

    def __init__(self, transport=None, mgmt_file=None):
        # mgmt_file (не mgmt_key_path): одноимённый параметр затенял
        # функцию модуля, и штатный конструктор без аргументов падал
        # TypeError 'NoneType' -- start и resume были сломаны.
        self._transport = transport if transport is not None else _Urllib()
        self._mgmt = mgmt_file if mgmt_file is not None else mgmt_key_path()

    def _bearer(self):
        with open(self._mgmt, encoding="utf-8") as f:
            return f.read().rstrip("\r\n")

    def fetch_list(self):
        # Отказ управленческого GET -- типизированное исключение: целый
        # http_status и reason из кода (без тела ответа). При неверном
        # ключе management GET идёт первым (до PUT), и без кода в блокере
        # оператор не узнаёт, что повторы ведут к блокировке адреса на
        # 30 минут.
        try:
            keys = self._transport.get_keys(self._bearer())
        except Exception as e:  # noqa: BLE001 -- нести код, не payload
            code = getattr(e, "code", None)
            if type(code) is int and 100 <= code <= 599:
                typed = ManagementRefused(
                    f"GET api-keys answered HTTP {code}")
                typed.http_status = code
                typed.reason = self.CODE_REASONS.get(code)
                raise typed from e
            raise
        if not isinstance(keys, list):
            raise RuntimeError("the key list answer is not a list")
        return keys

    # Причина выводится ИЗ КОДА, не из тела ответа (живой transport
    # возвращает только http_status): 400/422 -- валидация отвергла ДО
    # записи; 401/403 -- доступ; 500 -- исход неясен (write_failed может
    # примениться). Слово причины -- из конечного перечня REASONS #538.
    CODE_REASONS = {400: "invalid", 422: "invalid", 401: "unauthorized",
                    403: "forbidden"}

    def put_list(self, keys):
        why = self._transport.put_keys(self._bearer(), list(keys))
        if why is None:
            return ("applied", None)
        code = why.get("http_status")
        if code in self.CODE_REASONS:
            return ("refused", {"http_status": code,
                                "reason": self.CODE_REASONS[code]})
        raise RuntimeError(f"unclear PUT outcome (HTTP {code})")

    def probe(self, key):
        # Три исхода, не два: не-200/401x -- не «ключ отвергнут».
        # 502 у лежащего прокси или 500, зачитанные как False, проводили
        # негативную проверку авторизации ложно. Неясное -- исключение
        # оркестратору (библиотека превращает его в именованный блокер
        # без мутации).
        code = self._transport.models(key)
        if code == 200:
            return True
        if code in (401, 403):
            return False
        raise RuntimeError(f"probe /v1/models answered HTTP {code}, "
                           f"the outcome is unclear")

    def deliver(self, key, nodes):
        # Прямой write-канал по ЯВНОМУ сохранённому списку (#539): старый
        # push() решает по sha-гейту и settled -- резюмируемая раздача с
        # FAILED/NOT REACHED «завершилась» бы или молча пропустила. Итоги
        # сводит bus.results_from, строгую оценку -- strict_settled #538.
        # Блоб рендерит proxykey.blob -- ЕДИНСТВЕННЫЙ рендер secrets.env:
        # у двух писателей одного файла не может быть двух форматов.
        content = proxykey.blob({"MOP_PROXY_KEY": key})
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
            # Целочисленный .code: fetch_list поднимет типизированный
            # отказ (http_status), не строку.
            raise _HTTPStatus(f"GET api-keys answered HTTP {status}",
                              status)
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


class ManagementRefused(RuntimeError):
    """Отказ управления с типизированным целым кодом HTTP (без тела)."""


class _HTTPStatus(RuntimeError):
    """Транспортный отказ с целым кодом: живой GET, не строка в причине."""

    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправления запрещены: тихий уход с петли -- потеря контроля
    над тем, куда уехал Bearer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
