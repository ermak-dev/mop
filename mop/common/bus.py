"""Шина пула: синхронный фасад над NATS.

Почему фасад. Библиотека mop синхронная, а nats-py — только asyncio. Цикл
событий живёт в фоновом потоке, соединение одно на процесс и переживает все
вызовы: подключаться заново на каждый запрос значит вернуть себе ту же плату
за рукопожатие, ради ухода от которой шину и заводили.

Адресуем узел, а не аллокацию. `alloc exec` умер вместе со своей адресацией.
Узел мастер знает из ростера Nomad (`NodeName` аллокации) и шлёт запрос именно
туда. Маска и scatter-gather не нужны, и заодно не отвечает узел, на котором
остался протухший клон переехавшего папета.

Проект — первый токен субъекта. Один проект — один срез пула, и мастера разных
проектов не видят папетов друг друга:

    mop.admin.node.<узел>.rpc      оператор: все проекты плюс узловой disk
    mop.<проект>.node.<узел>.rpc     мастер проекта: state, states, sizes, tail,
                                   type, send, wipe, write
    mop.<проект>.node.<узел>.msg     папет проекта: state/states/tail/send
    mop.<проект>.all.msg             всем агентам сразу
    mop.<проект>.master.<id>.inbox   агент И папет -> конкретный мастер
    mop.<проект>.master.all.inbox    опрос: кто из мастеров проекта жив
    mop.<проект>.events              журнал проекта
    mop.<проект>.server.rpc          сервер: bootstrap песочницы (узел зовёт
                                   при старте), put (мастер кладёт файл)

`admin` — не проект, а его отсутствие: так ходит оператор из обычного шелла.
Узловой здесь только `disk`: место на хосте — факт про всех его жильцов.
`write` мастеру проекта отдан: оба файла его белого списка собираются на
управляющей машине, а не из проекта мастера, так что раздача кредов не даёт
одному проекту перезаписать креды другого. Полностью — в docs/BUS.md.

Инбокс адресуется мастером, а не проектом: два терминала, открытые в одном
проекте, иначе получали бы вести друг друга. Свой адрес мастер передаёт агенту
полем `reply_to`, а папету — конвертом каждого сообщения (`from-name`).

Обратный канал — тот же инбокс. Папет отвечает мастеру запросом в
`mop.<проект>.master.<id>.inbox`, и вердикт доставки приезжает ответом. Права на
это у папета были с самого начала (`mop.<проект>.master.>`), не было маршрута:
`send` знал два адреса — папет пула и сессия этого хоста, — а мастер живёт на
другом хосте и папетом не является. Отчёты папетов уходили в
LookupError и не доезжали никуда.

Разделение живёт в правах NATS-сервера (`deploy/nats-server.conf.j2`), агент лишь
повторяет его у себя: право, проверенное в одном месте, однажды окажется
проверенным ни в одном.
"""
import asyncio
import base64
import contextlib
import json
import os
import queue
import sys
import threading

try:
    import nats
    from nats.errors import NoRespondersError
except ImportError:
    # Отказ, а не выход (#169): библиотека не кончает процесс. Одну строку из
    # него делает тот, кто её импортировал (cli.run, `mop agent`).
    raise ImportError("bus library required: pip install --user "
                      "--break-system-packages nats-py") from None

from . import busnames, config as settings, creds  # noqa: E402

# Файл кредов, если его подсунули: врапер папета через tmux -e, `mop master`
# сессии мастера. Без него креды собирает config() из каталога сервера.
FILE = settings.get("MOP_BUS_CONFIG") or None
# Креды агента узла: их кладёт плейбук, и агент читает только их — каталог
# сервера с кредами мастера на машине в двух ролях ему не указ.
NODE_FILE = os.path.expanduser(busnames.NODE_FILE)
TIMEOUT = 20             # обычный запрос к агенту
MAX_PAYLOAD = 900_000    # под max_payload сервера (1 МБ) с запасом на конверт
ADMIN = busnames.ADMIN   # псевдопроект оператора: все проекты плюс узловой disk
ALL_MASTERS = busnames.ALL_MASTERS   # псевдо-id мастера: отвечают все

# Чей срез пула виден этому процессу. Ставит `mop master`, наследуют его
# потомки — в том числе mop mcp, запущенный сессией мастера.
PROJECT = settings.get("MOP_PROJECT") or ADMIN

_lock = threading.Lock()
_loop = None
_conn = None
_last_error = None


class BusError(RuntimeError):
    """Шина не довезла. Отдельный тип, потому что вызывающий обязан отличать
    «агент узла молчит» от «папет завис»: лечение у них разное.

    no_responders=True означает «нас пустили в субъект, но там никто не
    подписан» — это ответ сервера NATS, а не таймаут. Отличать обязательно:
    отсутствие подписчика и отсутствие ПРАВА писать выглядят одинаково
    тихо, а значат противоположное (mop/cli/service/mcp.py, is_master)."""

    def __init__(self, message, no_responders=False):
        super().__init__(message)
        self.no_responders = no_responders


class Refused(RuntimeError):
    """Сервис ответил, и ответ -- отказ (поле `error`, #146). Не BusError:
    шина довезла, и лечится это не у агента, а там, куда отказ показывает.
    RuntimeError -- чтобы диспетчер и loud в MCP сделали из него одну строку
    без отдельного знания о нём."""


def _load(path):
    """Файл кредов как его рендерит плейбук: {url, user, password}."""
    try:
        with open(path) as f:
            c = json.load(f)
    except ValueError as e:
        raise BusError(f"{path} is unreadable: {e}")
    if not c.get("url"):
        raise BusError(f"{path} has no url")
    return c


def config(file=None):
    """{url, user, password[, cafile]} этого процесса.

    Порядок: файл, который назвали (агент — свой узловой, врапер папета —
    через MOP_BUS_CONFIG), иначе сборка из каталога сервера (mop/common/creds.py)
    по MOP_SERVER_LAN и MOP_PROJECT. Третьего нет: файлы плейбука
    bus-master-<проект>.json ушли вместе с игрой мастера (#53).

    В url всегда LAN-адрес, никогда публичное имя: оно резолвится в адрес
    роутера, а хайрпин на порт шины роутер не делает — проверено, connection
    refused с обоих узлов.

    Файл узла и папета — nats:// на 4222 без TLS, и креды там единственное,
    что отделяет проект от проекта, — поэтому файл 0600 и по одному на
    проект. Каталог сервера — люди и сервисы сервера, они идут wss через
    TLS-прокси (#97); почему 4222 без TLS — комментарии в
    deploy/roles/bus/templates/nats-server.conf.j2."""
    path = file or FILE
    if path:
        try:
            return _load(path)
        except FileNotFoundError:
            raise BusError(f"no bus credentials: {path} — run mop server deploy")
    return server_config(settings.get("MOP_SERVER_LAN"))


def login():
    """Логин человека, под которым этот процесс на шине, или None.

    Им мастер называет себя владельцем задания (#161). Папет, узел и сервисы
    сервера -- не люди: их `send` аренду не берёт."""
    try:
        user = config().get("user") or ""
    except Exception:
        return None
    if not user or busnames.is_machine(user):
        return None
    return user


def server_config(host):
    """Креды этой машины на названном сервере -- из его каталога.

    Отдельно от config(), потому что `mop join` спрашивает и чужие серверы,
    на которые у машины уже есть вход: чей реестр знает этот клон (#125)."""
    directory = creds.server_dir(host)
    # Каталог сервера -- это человек или сервис сервера, и ходят они через
    # TLS-прокси (#97): пароль по сети открытым текстом не идёт. Кред один --
    # кто эта машина на шине (#106): одни креды на все проекты человека, а
    # какой именно проект -- решает СУБЪЕКТ, и права на субъект проверяет
    # сервер NATS. Ролевых паролей admin и master-<проект> больше нет.
    op = creds.operator(directory)
    if not op:
        raise BusError(f"no bus credentials: {directory}/{creds.OPERATOR_FILE} "
                       f"is missing — run mop join --server {host}")
    return creds.wss_config(host, settings.get("MOP_HTTPS_PORT"), PROJECT,
                            op["password"], user=op["user"],
                            cafile=creds.cafile(directory))


# ─── соединение ──────────────────────────────────────────────────────────
def auth(c):
    """Аргументы nats.connect по кредам из config(): один способ представиться
    шине на всех — фасад мастера, петля агента и его самопроверка."""
    out = {"servers": [c["url"]], "user": c.get("user"),
           "password": c.get("password")}
    # wss -- через TLS-прокси (#97); nats:// узлов и папетов -- без TLS.
    tls = creds.tls_context(c)
    if tls is not None:
        out["tls"] = tls
    return out


# ─── конверт и запрос: одно место (#264) ──────────────────────────────────
# Конверт {"verb": ..., **поля} собирался в пяти местах, и одно (ask_once)
# потеряло ensure_ascii=False; переход на прежний субъект (#207) был записан
# трижды. Здесь -- по одному разу на всех.
FLUSH = 5            # сколько ждать, пока публикация уйдёт на сервер


def encode(obj):
    """Тело сообщения шины: JSON в UTF-8, без \\u-экранирования."""
    return json.dumps(obj, ensure_ascii=False).encode()


def envelope(verb, **fields):
    """Конверт запроса к агенту или сервису: {"verb": глагол, **поля}."""
    return encode({"verb": verb, **fields})


async def arequest(nc, subj, data, timeout):
    """Запрос-ответ с переходом (#207): адресат ещё не слушает субъект с
    логином -- агент или сервис до раскатки, -- тогда прежний субъект.
    Уходит с уборкой перехода. -> сообщение; исключения nats -- как есть."""
    try:
        return await nc.request(subj, data, timeout=timeout)
    except NoRespondersError:
        if busnames.without_caller(subj) == subj:
            raise
        return await nc.request(busnames.without_caller(subj), data, timeout=timeout)


async def _quiet(_e):
    """error_cb разового соединения: отказ придёт исключением, stderr -- шум."""


@contextlib.contextmanager
def _connect_failure(c, error=lambda e: str(e)):
    """Отказ подключения -- BusError одной строкой с причиной (#130, #251):
    сырое исключение nats-py читалось бы трассой. c -- креды или функция,
    которая их достанет; error(e) -- текст ошибки для диагноза."""
    try:
        yield
    except BusError:
        raise
    except Exception as e:
        raise BusError(_why_not(c() if callable(c) else c, error(e)))


def _ensure_loop():
    global _loop
    if _loop is not None:
        return _loop
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True,
                     name="mop-bus").start()
    return _loop


def _call(coro, timeout):
    """Корутину — в фоновый цикл, результат — сюда. Таймаут берём с запасом:
    внутренний уже отработал и вернул понятную ошибку, а этот ловит только
    зависший цикл."""
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result(timeout + 5)


def _why_not(c, error):
    """Причина отказа подключения (#130): из ошибки попытки, которую nats-py
    отдал в error_cb, -- само исключение у него пустой NoServersError. Для
    TLS -- куда резолвится имя и чей сертификат предъявлен против
    закреплённого: чужой адрес иначе читался бы как неверный пароль."""
    import socket
    from urllib.parse import urlparse
    who = c.get("user") or "?"
    u = urlparse(c.get("url") or "")
    host, port = u.hostname or "?", u.port or (443 if u.scheme == "wss" else 4222)
    try:
        addr = socket.gethostbyname(host)
    except OSError:
        addr = None
    error = error or ""
    presented = pinned = None
    if "CERTIFICATE_VERIFY_FAILED" in error or "SSLCertVerificationError" in error:
        try:
            presented = creds.fingerprint(creds.peer_cert(host, port))
        except OSError:
            pass
        if c.get("cafile"):
            try:
                import ssl
                with open(c["cafile"]) as f:
                    pinned = creds.fingerprint(ssl.PEM_cert_to_DER_cert(f.read()))
            except (OSError, ValueError):
                pass
    return creds.connect_failure(who, error, host, port, addr, presented, pinned)


async def _on_error(e):
    """Отказ прав NATS приезжает сюда, а не в ответ на запрос: сервер молча
    не доставляет публикацию, и запрос честно висит до таймаута. Без этого
    «нет права писать в mop.node.X.rpc» читалось бы как «агент молчит 20с» —
    диагноз, ведущий чинить работающий узел."""
    global _last_error
    _last_error = str(e)
    for fn in list(ERROR_LISTENERS):
        try:
            fn(_last_error)
        except Exception:
            pass


# Кому ещё нужны асинхронные ошибки шины (#213): MCP мастера узнаёт отсюда,
# что шина отказала ему в подписке на свой инбокс. fn(текст) -> None, зовётся
# из фонового цикла; библиотека сама ничего не печатает.
ERROR_LISTENERS = []


async def _open(**opts):
    """Соединение, которое при отказе закрывается само (#251).

    nats.connect() при отказе с allow_reconnect=False бросает до закрытия
    транспорта; websocket-транспорт nats-py держит aiohttp.ClientSession, и
    при выходе процесса сборщик печатал «Unclosed client session» -- после
    запроса пароля `mop join` успех был неотличим от ошибки. Клиент
    заводится здесь, и закрыть его есть кому в любом исходе."""
    nc = nats.NATS()
    try:
        await nc.connect(**opts)
    except BaseException:
        try:
            await nc.close()
        except Exception:
            pass
        raise
    return nc


async def _aconnect(file=None):
    return await _open(
        **auth(config(file)), name="mop", error_cb=_on_error,
        # Молча копить неотправленное в ожидании сервера — худший вид отказа:
        # вызывающий получит успех, которого не было.
        allow_reconnect=True, max_reconnect_attempts=-1,
        reconnect_time_wait=2, connect_timeout=5,
    )


def check(c):
    """Пустить ли нас шина с этими кредами. Отказ — исключение.

    Одноразовое соединение без реконнекта: здесь проверяют пароль, и
    бесконечные попытки превратили бы неверный пароль в зависание вместо
    ответа. Зовёт `mop join` до того, как что-то запишет (#84)."""
    seen = []

    async def quiet(e):
        seen.append(str(e))   # причина отказа (#130); в stderr -- шум

    async def once():
        nc = await _open(**auth(c), name="mop-join", error_cb=quiet,
                         allow_reconnect=False, connect_timeout=5)
        await nc.close()
    with _connect_failure(c, lambda e: (seen[-1] if seen else "") or str(e)):
        _call(once(), 15)


def ask_once(c, subj, verb, timeout=5, **fields):
    """Один запрос отдельным соединением по кредам c. -> ответ (dict).

    Для серверов, которые не текущие: общее соединение процесса привязано к
    одному серверу, а `mop join` спрашивает все, где у машины есть вход."""
    async def once():
        nc = await _open(**auth(c), name="mop-join", error_cb=_quiet,
                         allow_reconnect=False, connect_timeout=timeout)
        try:
            msg = await arequest(nc, subj, envelope(verb, **fields), timeout)
            return json.loads(msg.data.decode())
        finally:
            await nc.close()
    with _connect_failure(c):
        return _call(once(), timeout * 3)


def connect(file=None):
    """Соединение процесса. Ленивое: фронтенды, которым шина не нужна
    (`mop add`, `mop delete`), не должны падать от её недоступности.

    file — чьи креды: узел из процесса задачи Nomad (`mop driver run`)
    представляется своим узловым файлом, а не каталогом сервера, которого
    на узле нет."""
    global _conn
    with _lock:
        if _conn is not None and not _conn.is_closed:
            return _conn
        def creds_or_nothing():
            try:
                return config(file)
            except Exception:
                return {}
        with _connect_failure(creds_or_nothing, lambda e: _last_error or str(e)):
            _conn = _call(_aconnect(file), 10)
        return _conn


def close():
    global _conn
    with _lock:
        if _conn is not None and not _conn.is_closed:
            try:
                _call(_conn.drain(), 5)
            except Exception:
                pass
        _conn = None


# ─── субъекты ────────────────────────────────────────────────────────────
def subject(node, channel="rpc", project=None):
    """Агент узла. Человек спрашивает rpc со своим логином в субъекте (#207):
    публиковать туда NATS даёт только ему, и агент берёт вызывающего оттуда.
    Машина (login() -- None) спрашивает прежним субъектом."""
    return busnames.node(project or PROJECT, node, channel, login=login())


def broadcast(project=None):
    """Все агенты разом. Нужен там, где спрашивающий не знает состава пула."""
    return busnames.broadcast(project or PROJECT)


def inbox(master_id, project=None):
    """Адрес мастера: сюда ему пишут агенты узлов И папеты проекта.

    Обратный канал не заводил себе отдельного субъекта намеренно: у мастера
    уже есть ровно одно место, куда ему кладут вести, и права на него у
    папета уже были (`mop.<проект>.master.>` в его creds). Отдельный субъект
    означал бы второй ответ на вопрос «куда писать мастеру» — и правку прав
    на сервере ради того, что и так разрешено.

    master_id=ALL_MASTERS — не адрес, а опрос: отвечают все живые мастера
    проекта. Так папет узнаёт, кому он может ответить, не имея ростера."""
    return busnames.inbox(project or PROJECT, master_id)


def events(project=None):
    return busnames.events(project or PROJECT)


def cluster_subject(project=None, as_login=None):
    """Сервис кластера как адресат (#80): Nomad за шиной.

    Свой субъект, а не `server.rpc`: в субъект сервера имеет право писать
    агент узла (bootstrap песочницы), и глаголы над Nomad там означали бы,
    что джобы регистрирует и снимает любой узел. Здесь прав ни у кого не
    прибавляется: у `master-<проект>` уже есть весь `mop.<проект>.>`, а у
    папета и узла его нет.

    Логин человека -- токеном субъекта (#207), как у агента; as_login --
    чужие креды (`mop join` спрашивает другие серверы)."""
    return busnames.cluster(project or PROJECT, login=as_login or login())


def build_subject():
    """Сборщик образов (#123): только оператору -- образ собирается кодом
    проекта на гипервизорах."""
    return busnames.build()


def server_subject(project=None):
    """Сервер как адресат (#62): bootstrap песочниц и хранение их файлов.
    Первый токен — проект, как у всех: узел пишет за папета своего проекта,
    мастер — за свой проект, а права NATS делят так же, как везде."""
    return busnames.server(project or PROJECT)


# ─── запросы ─────────────────────────────────────────────────────────────


def _silence(who, timeout):
    """Почему тихо. Отличать «нет прав» от «агент лёг» обязательно: лечение
    у них разное и противоположное по стоимости ошибки."""
    if _last_error and "permissions violation" in _last_error.lower():
        return f"bus did not let the request through — {who}: {_last_error}"
    return f"{who} did not answer in {timeout}s"


def _ask(subj, who, dead, verb, timeout, **fields):
    """Запрос-ответ в субъект. -> разобранный ответ (dict).

    Кто на том конце — знает только вызывающий, поэтому имя адресата (`who`) и
    смысл его молчания (`dead`) приезжают параметрами. Разница не косметическая:
    «юнит mop-agent не работает» и «мастер закрыл сессию» лечатся по-разному, а
    единственное, что их различает, — субъект, в который спрашивали."""
    payload = envelope(verb, **fields)
    if len(payload) > MAX_PAYLOAD:
        raise BusError(f"request {verb} exceeds the bus limit "
                       f"({len(payload)} > {MAX_PAYLOAD} bytes)")
    nc = connect()
    try:
        msg = _call(arequest(nc, subj, payload, timeout), timeout)
    except NoRespondersError:
        raise BusError(dead, no_responders=True)
    except asyncio.TimeoutError:
        raise BusError(_silence(who, timeout))
    except Exception as e:
        raise BusError(f"{who}: {e}")
    try:
        return json.loads(msg.data.decode())
    except ValueError:
        raise BusError(f"{who} answered with non-JSON: {msg.data[:120]!r}")


def request(node, verb, timeout=TIMEOUT, channel="rpc", project=None, **fields):
    """Глагол агенту узла. -> разобранный ответ (dict).

    `project` — явный параметр, а не поле запроса: адрес живёт в субъекте, и
    попади он в тело, права NATS его бы не увидели. Обычно не нужен: процесс
    ходит своим проектом, который ему поставил `mop master`.

    Ошибка агента приезжает полем `error` внутри ответа и не поднимает
    исключение: это ответ, а не отказ шины. Исключение — только когда до
    агента не доехали."""
    return _ask(subject(node, channel, project), f"node agent {node}",
                f"node agent {node} is not subscribed — the mop-agent unit is not running",
                verb, timeout, **fields)


def ask(master_id, verb, timeout=TIMEOUT, project=None, **fields):
    """Глагол мастеру, в его инбокс. -> разобранный ответ (dict).

    Обратная сторона канала: папет отвечает тому, кто его послал. Запрос-ответ,
    а не публикация, ровно по той же причине, по какой её выбрали для `send`:
    отправителю нужен вердикт доставки, а не факт отправки. Публикация в инбокс
    мёртвого мастера выглядела бы успехом — а это тишина, то есть худший исход
    для петли, где отчёт папета и есть главный сигнал."""
    return _ask(inbox(master_id, project), f"master {master_id}",
                f"master {master_id} is not on the bus: session closed or the address "
                f"isn't its own — see mcp__mop__agents",
                verb, timeout, **fields)


def can_login(user, password, port, host="127.0.0.1", tries=10):
    """Пускает ли шина этого пользователя. Громко, если нет.

    Отдельным соединением и с повторами: SIGHUP nats-server отрабатывает не
    мгновенно, а о битом конфиге не сообщает вовсе (#117) -- подключение и
    есть проверка, что новый пользователь заведён."""
    import time
    import nats

    async def once():
        nc = await _open(servers=[f"nats://{host}:{port}"], user=user,
                         password=password, connect_timeout=2,
                         allow_reconnect=False, max_reconnect_attempts=0,
                         error_cb=_quiet)
        await nc.close()

    last = None
    for _ in range(tries):
        try:
            asyncio.run(once())
            return
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise BusError(f"{user} cannot log in to the bus: {last or 'refused'}")


def ask_cluster(verb, timeout=TIMEOUT, project=None, **fields):
    """Глагол сервису кластера (mop-cluster). -> разобранный ответ (dict)."""
    return _ask(cluster_subject(project), "cluster service",
                "no cluster service is subscribed — the mop-cluster unit is "
                "not running on the server",
                verb, timeout, **fields)


def call_cluster(verb, timeout=TIMEOUT, project=None, **fields):
    """Глагол сервису кластера с громким отказом. -> ответ | Refused.

    Отказ -- исключение, а не поле: около двадцати мест проверяли его сами и
    сообщали четырьмя способами, а MCP держал свою копию, в которой причина
    терялась (#146)."""
    got = ask_cluster(verb, timeout=timeout, project=project, **fields)
    if got.get("error"):
        raise Refused(got["error"])
    return got


def ask_server(verb, timeout=TIMEOUT, project=None, **fields):
    """Глагол серверу (mop-bootstrap). -> разобранный ответ (dict)."""
    return _ask(server_subject(project), "bootstrap service",
                "no bootstrap service is subscribed — the mop-bootstrap unit is "
                "not running on the server",
                verb, timeout, **fields)


async def _one(nc, node, data, timeout, channel, project):
    """Один запрос узлу внутри цикла. -> ответ | BusError.

    Общая часть request_many и request_stream: ошибка возвращается, а не
    бросается — один молчащий узел не должен уносить с собой картину по
    остальным."""
    try:
        msg = await arequest(nc, subject(node, channel, project), data, timeout)
        return json.loads(msg.data.decode())
    except NoRespondersError:
        return BusError(f"node agent {node} is not subscribed")
    except asyncio.TimeoutError:
        return BusError(_silence(f"node agent {node}", timeout))
    except Exception as e:
        return BusError(f"{node}: {e}")


UNREACHED, FAILED = "unreached", "failed"


def verdict(answer):
    """Ответ узла из request_many -> (UNREACHED, причина | None) -- до агента
    не доехали или он промолчал, (FAILED, причина) -- агент отказал полем
    error, None -- ответ есть. Одна тройная проверка на всех: её держали
    bus.failure и keys.results_from (ныне bus.results_from) каждый у себя
    (#264)."""
    if isinstance(answer, Exception):
        return UNREACHED, str(answer)
    if answer is None:
        return UNREACHED, None
    if answer.get("error"):
        return FAILED, str(answer["error"])
    return None


def results_from(nodes, answers):
    """Ответы агентов на `write` -> {узел: "OK" | "FAILED: …" | "NOT REACHED: …"}.
    Чистая функция (#135): молчание агента называется молчанием, без
    отсылки к токену Nomad и контроллеру. Одна на клиента и сервер (#315):
    копии держали client/keys.py и server/credreg.py.

    Отказ тела агент отдаёт полем failed {тело: причина}, а не error (#366):
    узел ответил, но записи нет. Такой узел -- FAILED с причиной первого
    тела; строки в written не разбираются, их формулировка не контракт."""
    out = {}
    for node in nodes:
        answer = answers.get(node)
        got = verdict(answer)
        if got is None and answer.get("failed"):
            body, why = next(iter(answer["failed"].items()))
            out[node] = f"FAILED: {body}: {why}"[:128]
        elif got is None:
            out[node] = "OK"
        elif got[0] == UNREACHED:
            out[node] = f"NOT REACHED: {got[1] or 'no answer'}"
        else:
            out[node] = f"FAILED: {got[1][:120]}"
    return out


# Запись в тела pve-узла идёт секундами на тело (#136, #137): таймаут --
# с запасом, иначе живой агент читался бы молчащим.
WRITE_TIMEOUT = 60


def as_file(path, text_or_bytes):
    """Файл для глагола `write`: (путь, содержимое в base64 строкой) -- JSON
    байтов не везёт. Обратная сторона -- file_data у агента (#315)."""
    raw = text_or_bytes.encode() if isinstance(text_or_bytes, str) else text_or_bytes
    return (path, base64.b64encode(raw).decode())


def file_data(b64):
    """Содержимое файла из глагола `write` -> байты (пара к as_file)."""
    return base64.b64decode(b64)


def failure(answer):
    """Почему ответ узла не годится: текст либо None, если ответ есть.

    Три исхода у каждого ответа request_many — исключение шины, пустота,
    поле error от агента, — и каждый командлет разбирал их сам. Пустой
    ответ ({}) здесь -- тоже молчание, как было."""
    got = verdict(answer)
    if got:
        return got[1] or "no response"
    return None if answer else "no response"


def _per_node(nodes, fields):
    """Узлы request_many -> {узел: поля}: список -- общие поля всем,
    словарь {узел: свои поля} -- свои поверх общих."""
    if isinstance(nodes, dict):
        return {n: {**fields, **(own or {})} for n, own in nodes.items()}
    return {n: dict(fields) for n in nodes}


def request_many(verb, nodes, timeout=TIMEOUT, channel="rpc", project=None, **fields):
    """Один глагол многим узлам, параллельно по одному соединению.

    Ради этого всё и затевалось: раньше состояние пула стоило по четыре
    рукопожатия exec'а на папета подряд. Форма та же, что у request (#264):
    глагол и поля. nodes -- список узлов (поля общие), либо {узел: свои
    поля}, когда каждого спрашивают о своём (states -- о своих папетах);
    общие поля идут всем, свои -- поверх.

    -> {узел: ответ | BusError}. Ошибка возвращается, а не бросается: один
    молчащий узел не должен уносить с собой картину по остальным."""
    asks = _per_node(nodes, fields)
    if not asks:
        return {}
    nc = connect()
    names = list(asks)

    async def all_of():
        return await asyncio.gather(
            *(_one(nc, n, envelope(verb, **asks[n]), timeout, channel, project)
              for n in names))

    return dict(zip(names, _call(all_of(), timeout)))


def request_stream(verb, asks, timeout=TIMEOUT, channel="rpc", project=None, **fields):
    """Как request_many, но пары (ключ, ответ) отдаются по мере готовности.

    Нужен там, где ответ показывают сразу, а не собирают в таблицу: обмер
    места одного папета занимает секунды, и ждать самого медленного, чтобы
    показать первого, незачем. Ключ отдельно от узла именно поэтому — вопросов
    к одному узлу может быть несколько, по одному на папета, а request_many
    ключуется узлом и такого не умеет.

    asks: {ключ: (узел, свои поля)}; общие поля -- всем, свои поверх.
    -> генератор (ключ, ответ | BusError). Ошибка приезжает значением, а не
    броском: один молчащий узел не должен уносить с собой картину по
    остальным.
    """
    if not asks:
        return
    nc = connect()
    done = queue.Queue()

    async def one(key, node, own):
        data = envelope(verb, **{**fields, **(own or {})})
        done.put((key, await _one(nc, node, data, timeout, channel, project)))

    async def all_of():
        await asyncio.gather(*(one(k, n, own) for k, (n, own) in asks.items()))

    fut = asyncio.run_coroutine_threadsafe(all_of(), _ensure_loop())
    try:
        for _ in range(len(asks)):
            yield done.get(timeout=timeout + 5)
    finally:
        # Генератор могли бросить недочитанным (Ctrl-C, `| head`) — фоновые
        # запросы в этом случае дожидаться некому, и цикл остался бы с ними.
        fut.cancel()


def ask_stream(subj, who, verb, first=10, idle=120, **fields):
    """Долгий запрос с ходом работы (#123). -> генератор событий (dict);
    последнее несёт done.

    Обычный request-reply отвечает одним сообщением, а сборка идёт минутами:
    ответчик шлёт в инбокс просителя шаги и сердцебиение, итог -- последним.
    Первое событие ждём first секунд (нет подписчика -- publish молчит, и
    без этого отказ читался бы как долгая сборка), дальше -- idle между
    событиями: ответчик шлёт сердцебиение, и тишина значит, что он умер."""
    nc = connect()
    got = queue.Queue()
    payload = envelope(verb, **fields)

    async def start():
        inbox = nc.new_inbox()

        async def on_msg(msg):
            try:
                got.put(json.loads(msg.data.decode()))
            except ValueError:
                pass
        sub = await nc.subscribe(inbox, cb=on_msg)
        await nc.publish(subj, payload, reply=inbox)
        await nc.flush(timeout=FLUSH)
        return sub

    sub = _call(start(), 10)
    wait = first
    try:
        while True:
            try:
                ev = got.get(timeout=wait)
            except queue.Empty:
                raise BusError(f"no answer from the {who} for {wait}s"
                               + (" — is it running on the server?" if wait == first else ""))
            wait = idle
            yield ev
            if ev.get("done"):
                return
    finally:
        try:
            _call(sub.unsubscribe(), 5)
        except Exception:
            pass


def gather(verb, timeout=5, subj=None, **fields):
    """Разослать глагол всем агентам и собрать, кто отзовётся. -> [ответ].

    Нужен там, где спрашивающий не знает списка узлов: узловой mop mcp живёт
    без токена Nomad, а значит и без ростера. Мастеру это не нужно — у него
    ростер богаче (аллокации, llm, origin), и он адресует узлы поимённо.

    Ответов ждём до таймаута, а не до заранее известного числа: сколько в пуле
    узлов, здесь неизвестно принципиально — в этом и смысл вызова."""
    subj = subj or broadcast()
    nc = connect()
    payload = envelope(verb, **fields)

    async def run():
        inbox = nc.new_inbox()
        sub = await nc.subscribe(inbox)
        await nc.publish(subj, payload, reply=inbox)
        await nc.flush(timeout=FLUSH)
        out, deadline = [], asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                break
            try:
                msg = await sub.next_msg(timeout=left)
            except Exception:
                break
            try:
                out.append(json.loads(msg.data.decode()))
            except ValueError:
                continue
        await sub.unsubscribe()
        return out

    return _call(run(), timeout)


def subscribe(subj, handler):
    """Постоянная подписка. handler(dict) -> ответ|None.

    Отвечаем, если спрашивают. Подписка на инбокс — это не только «мне
    положили весть»: тем же субъектом папет шлёт мастеру отчёт и ждёт вердикта
    доставки. Агент кладёт вести публикацией и `reply` не ставит — для него
    ничего не меняется.

    handler зовётся из фонового цикла и обязан быть быстрым; бросить он теперь
    может — исключение уедет спрашивающему ответом, а не пропадёт в тишине."""
    nc = connect()

    # Корутина, а не функция: nats-py обычный callback не принимает.
    async def cb(msg):
        try:
            out = handler(json.loads(msg.data.decode()))
        except Exception as e:
            out = {"error": str(e)}
        if not msg.reply:
            return
        try:
            await msg.respond(encode(out or {}))
        except Exception:
            pass

    return _call(nc.subscribe(subj, cb=cb), 10)
