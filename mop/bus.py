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
import json
import os
import queue
import sys
import threading

try:
    import nats
    from nats.errors import NoRespondersError
except ImportError:
    sys.exit("bus library required: pip install --user --break-system-packages nats-py")

from . import config as settings, creds  # noqa: E402

# Файл кредов, если его подсунули: врапер папета через tmux -e, `mop master`
# сессии мастера. Без него креды собирает config() из каталога сервера.
FILE = os.environ.get("MOP_BUS_CONFIG")
# Креды агента узла: их кладёт плейбук, и агент читает только их — каталог
# сервера с кредами мастера на машине в двух ролях ему не указ.
NODE_FILE = os.path.expanduser("~/.config/mop/bus.json")
TIMEOUT = 20             # обычный запрос к агенту
MAX_PAYLOAD = 900_000    # под max_payload сервера (1 МБ) с запасом на конверт
ADMIN = creds.ADMIN      # псевдопроект оператора: все проекты плюс узловой disk
CLUSTER_CHANNEL = "cluster"   # токен субъекта сервиса кластера (mop/cluster.py)
ALL_MASTERS = "all"      # псевдо-id мастера: инбокс, на котором отвечают все

# Чей срез пула виден этому процессу. Ставит `mop master`, наследуют его
# потомки — в том числе mop mcp, запущенный сессией мастера.
PROJECT = os.environ.get("MOP_PROJECT") or ADMIN

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
    через MOP_BUS_CONFIG), иначе сборка из каталога сервера (mop/creds.py)
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
            raise BusError(f"no bus credentials: {path} — run mop deploy")
    return server_config(settings.get("MOP_SERVER_LAN"))


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


async def _on_error(e):
    """Отказ прав NATS приезжает сюда, а не в ответ на запрос: сервер молча
    не доставляет публикацию, и запрос честно висит до таймаута. Без этого
    «нет права писать в mop.node.X.rpc» читалось бы как «агент молчит 20с» —
    диагноз, ведущий чинить работающий узел."""
    global _last_error
    _last_error = str(e)


async def _aconnect(file=None):
    return await nats.connect(
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
    async def quiet(_e):
        pass          # отказ вернётся исключением; трассировка в stderr — шум

    async def once():
        nc = await nats.connect(**auth(c), name="mop-join", error_cb=quiet,
                                allow_reconnect=False, connect_timeout=5)
        await nc.close()
    _call(once(), 15)


def ask_once(c, subj, verb, timeout=5, **fields):
    """Один запрос отдельным соединением по кредам c. -> ответ (dict).

    Для серверов, которые не текущие: общее соединение процесса привязано к
    одному серверу, а `mop join` спрашивает все, где у машины есть вход."""
    async def quiet(_e):
        pass

    async def once():
        nc = await nats.connect(**auth(c), name="mop-join", error_cb=quiet,
                                allow_reconnect=False, connect_timeout=timeout)
        try:
            msg = await nc.request(subj, json.dumps({"verb": verb, **fields}).encode(),
                                   timeout=timeout)
            return json.loads(msg.data.decode())
        finally:
            await nc.close()
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
        try:
            _conn = _call(_aconnect(file), 10)
        except BusError:
            raise
        except Exception as e:
            # Имя пользователя обязательно в тексте: отказ «Authorization
            # Violation» приезжает из nats-py пустой строкой, и без имени
            # отозванный доступ (#84) читается как «сервер лёг».
            try:
                who = config(file).get("user") or "?"
            except Exception:
                who = "?"
            why = str(e) or ("wrong password, or the bus does not know this "
                             "user any more")
            raise BusError(f"no connection to bus as {who}: {why}")
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
    return f"mop.{project or PROJECT}.node.{node}.{channel}"


def broadcast(project=None):
    """Все агенты разом. Нужен там, где спрашивающий не знает состава пула."""
    return f"mop.{project or PROJECT}.all.msg"


def inbox(master_id, project=None):
    """Адрес мастера: сюда ему пишут агенты узлов И папеты проекта.

    Обратный канал не заводил себе отдельного субъекта намеренно: у мастера
    уже есть ровно одно место, куда ему кладут вести, и права на него у
    папета уже были (`mop.<проект>.master.>` в его creds). Отдельный субъект
    означал бы второй ответ на вопрос «куда писать мастеру» — и правку прав
    на сервере ради того, что и так разрешено.

    master_id=ALL_MASTERS — не адрес, а опрос: отвечают все живые мастера
    проекта. Так папет узнаёт, кому он может ответить, не имея ростера."""
    return f"mop.{project or PROJECT}.master.{master_id}.inbox"


def events(project=None):
    return f"mop.{project or PROJECT}.events"


def cluster_subject(project=None):
    """Сервис кластера как адресат (#80): Nomad за шиной.

    Свой субъект, а не `server.rpc`: в субъект сервера имеет право писать
    агент узла (bootstrap песочницы), и глаголы над Nomad там означали бы,
    что джобы регистрирует и снимает любой узел. Здесь прав ни у кого не
    прибавляется: у `master-<проект>` уже есть весь `mop.<проект>.>`, а у
    папета и узла его нет."""
    return f"mop.{project or PROJECT}.{CLUSTER_CHANNEL}.rpc"


def build_subject():
    """Сборщик образов (#123): только оператору -- образ собирается кодом
    проекта на гипервизорах."""
    return f"mop.{ADMIN}.build.rpc"


def server_subject(project=None):
    """Сервер как адресат (#62): bootstrap песочниц и хранение их файлов.
    Первый токен — проект, как у всех: узел пишет за папета своего проекта,
    мастер — за свой проект, а права NATS делят так же, как везде."""
    return f"mop.{project or PROJECT}.server.rpc"


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
    payload = json.dumps({"verb": verb, **fields}, ensure_ascii=False).encode()
    if len(payload) > MAX_PAYLOAD:
        raise BusError(f"request {verb} exceeds the bus limit "
                       f"({len(payload)} > {MAX_PAYLOAD} bytes)")
    nc = connect()
    try:
        msg = _call(nc.request(subj, payload, timeout=timeout), timeout)
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
        async def quiet(_e):
            pass
        nc = await nats.connect(servers=[f"nats://{host}:{port}"], user=user,
                                password=password, connect_timeout=2,
                                allow_reconnect=False, max_reconnect_attempts=0,
                                error_cb=quiet)
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


def ask_server(verb, timeout=TIMEOUT, project=None, **fields):
    """Глагол серверу (mop-bootstrap). -> разобранный ответ (dict)."""
    return _ask(server_subject(project), "bootstrap service",
                "no bootstrap service is subscribed — the mop-bootstrap unit is "
                "not running on the server",
                verb, timeout, **fields)


async def _one(nc, node, req, timeout, channel, project):
    """Один запрос узлу внутри цикла. -> ответ | BusError.

    Общая часть request_many и request_stream: ошибка возвращается, а не
    бросается — один молчащий узел не должен уносить с собой картину по
    остальным."""
    try:
        msg = await nc.request(
            subject(node, channel, project),
            json.dumps(req, ensure_ascii=False).encode(), timeout=timeout)
        return json.loads(msg.data.decode())
    except NoRespondersError:
        return BusError(f"node agent {node} is not subscribed")
    except asyncio.TimeoutError:
        return BusError(_silence(f"node agent {node}", timeout))
    except Exception as e:
        return BusError(f"{node}: {e}")


def failure(answer):
    """Почему ответ узла не годится: текст либо None, если ответ есть.

    Три исхода у каждого ответа request_many — исключение шины, пустота,
    поле error от агента, — и каждый командлет разбирал их сам."""
    if isinstance(answer, Exception):
        return str(answer)
    if not answer:
        return "no response"
    return answer.get("error") or None


def request_many(requests, timeout=TIMEOUT, channel="rpc", project=None):
    """Разные запросы разным узлам, параллельно по одному соединению.

    Ради этого всё и затевалось: раньше состояние пула стоило по четыре
    рукопожатия exec'а на папета подряд. Запрос у каждого узла свой — он
    спрашивается о своих папетах, — поэтому на входе {узел: {verb, **поля}},
    а не общий глагол.

    -> {узел: ответ | BusError}. Ошибка возвращается, а не бросается: один
    молчащий узел не должен уносить с собой картину по остальным."""
    if not requests:
        return {}
    nc = connect()
    nodes = list(requests)

    async def all_of():
        return await asyncio.gather(
            *(_one(nc, n, requests[n], timeout, channel, project) for n in nodes))

    return dict(zip(nodes, _call(all_of(), timeout)))


def request_stream(requests, timeout=TIMEOUT, channel="rpc", project=None):
    """Как request_many, но пары (ключ, ответ) отдаются по мере готовности.

    Нужен там, где ответ показывают сразу, а не собирают в таблицу: обмер
    места одного папета занимает секунды, и ждать самого медленного, чтобы
    показать первого, незачем. Ключ отдельно от узла именно поэтому — вопросов
    к одному узлу может быть несколько, по одному на папета, а request_many
    ключуется узлом и такого не умеет.

    requests: {ключ: (узел, запрос)}. -> генератор (ключ, ответ | BusError).
    Ошибка приезжает значением, а не броском: один молчащий узел не должен
    уносить с собой картину по остальным.
    """
    if not requests:
        return
    nc = connect()
    done = queue.Queue()

    async def one(key, node, req):
        done.put((key, await _one(nc, node, req, timeout, channel, project)))

    async def all_of():
        await asyncio.gather(*(one(k, n, r) for k, (n, r) in requests.items()))

    fut = asyncio.run_coroutine_threadsafe(all_of(), _ensure_loop())
    try:
        for _ in range(len(requests)):
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
    payload = json.dumps({"verb": verb, **fields}, ensure_ascii=False).encode()

    async def start():
        inbox = nc.new_inbox()

        async def on_msg(msg):
            try:
                got.put(json.loads(msg.data.decode()))
            except ValueError:
                pass
        sub = await nc.subscribe(inbox, cb=on_msg)
        await nc.publish(subj, payload, reply=inbox)
        await nc.flush(timeout=5)
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
    payload = json.dumps({"verb": verb, **fields}, ensure_ascii=False).encode()

    async def run():
        inbox = nc.new_inbox()
        sub = await nc.subscribe(inbox)
        await nc.publish(subj, payload, reply=inbox)
        await nc.flush(timeout=5)
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


def publish(subj, **fields):
    nc = connect()
    _call(nc.publish(subj, json.dumps(fields, ensure_ascii=False).encode()), 5)
    _call(nc.flush(timeout=5), 5)


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
            await msg.respond(json.dumps(out or {}, ensure_ascii=False).encode())
        except Exception:
            pass

    return _call(nc.subscribe(subj, cb=cb), 10)
