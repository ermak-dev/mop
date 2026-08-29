"""Шина пула: синхронный фасад над NATS.

Почему фасад. Библиотека mop синхронная, а nats-py — только asyncio. Цикл
событий живёт в фоновом потоке, соединение одно на процесс и переживает все
вызовы: подключаться заново на каждый запрос значит вернуть себе ту же плату
за рукопожатие, ради ухода от которой шину и заводили.

АДРЕСУЕМ УЗЕЛ, А НЕ АЛЛОКАЦИЮ. `alloc exec` умер вместе со своей адресацией.
Узел мастер знает из ростера Nomad (`NodeName` аллокации) и шлёт запрос именно
туда. Маска и scatter-gather не нужны, и заодно не отвечает узел, на котором
остался протухший клон переехавшего слейва.

ШАРД — ПЕРВЫЙ ТОКЕН СУБЪЕКТА. Один проект — один срез пула, и мастера разных
проектов не видят слейвов друг друга:

    mop.admin.node.<узел>.rpc      всё и везде: узловые глаголы, все шарды
    mop.<шард>.node.<узел>.rpc     мастер шарда: state/states/tail/type/send
    mop.<шард>.node.<узел>.msg     слейв шарда: state/states/tail/send
    mop.<шард>.all.msg             всем агентам сразу
    mop.<шард>.master.<id>.inbox   агент И слейв -> КОНКРЕТНЫЙ мастер
    mop.<шард>.master.all.inbox    опрос: кто из мастеров шарда жив
    mop.<шард>.events              журнал шарда

`admin` — не шард, а его отсутствие: так ходит оператор из обычного шелла.
Узловые операции (раздача кредов) живут только там. Отдать их мастерам значило
бы позволить мастеру проекта A перезаписать креды, которыми живёт проект B, —
файл-то узловой.

Инбокс адресуется МАСТЕРОМ, а не шардом: два терминала, открытые в одном
проекте, иначе получали бы вести друг друга. Свой адрес мастер передаёт агенту
полем `reply_to`, а слейву — конвертом каждого сообщения (`from-name`).

ОБРАТНЫЙ КАНАЛ — ТОТ ЖЕ ИНБОКС. Слейв отвечает мастеру запросом в
`mop.<шард>.master.<id>.inbox`, и вердикт доставки приезжает ответом. Права на
это у слейва были с самого начала (`mop.<шард>.master.>`), не было МАРШРУТА:
`send` знал два адреса — слейв пула и сессия ЭТОГО хоста, — а мастер живёт на
другом хосте и слейвом не является. Отчёты слейвов уходили в
LookupError и не доезжали никуда.

Разделение живёт в правах NATS-сервера (`deploy/nats-server.conf.j2`), агент лишь
повторяет его у себя: право, проверенное в одном месте, однажды окажется
проверенным ни в одном.
"""
import asyncio
import json
import os
import sys
import threading

try:
    import nats
    from nats.errors import NoRespondersError
except ImportError:
    sys.exit("нужна библиотека шины: pip install --user --break-system-packages nats-py")

CONFIG = os.environ.get("MOP_BUS_CONFIG") or os.path.expanduser("~/.config/mop/bus.json")
TIMEOUT = 20             # обычный запрос к агенту
MAX_PAYLOAD = 900_000    # под max_payload сервера (1 МБ) с запасом на конверт
ADMIN = "admin"          # псевдошард оператора: все шарды плюс узловые глаголы
ALL_MASTERS = "all"      # псевдо-id мастера: инбокс, на котором отвечают ВСЕ

# Чей срез пула виден этому процессу. Ставит `mop master`, наследуют его
# потомки — в том числе mop mcp, запущенный сессией мастера.
SHARD = os.environ.get("MOP_SHARD") or ADMIN

_lock = threading.Lock()
_loop = None
_conn = None
_last_error = None


class BusError(RuntimeError):
    """Шина не довезла. Отдельный тип, потому что вызывающий обязан отличать
    «агент узла молчит» от «слейв завис»: лечение у них разное."""


def config():
    """{url, user, password, tls_hostname} — раскатывается ansible'ом.

    `tls_hostname` отделяет, КУДА подключаться, от того, ЧЕЙ сертификат ждать.
    Это не тонкость, а необходимость: узлы пула сидят в той же локалке, что и
    сервер шины, но публичное имя кластера обычно резолвится в адрес
    роутера, и хайрпин на 4222 он не делает — проверено, connection refused с
    обоих узлов. Ходим на LAN-адрес, а сертификат проверяем по имени, на
    которое он и выписан.

    Отдельный файл, а не переменные окружения: искать креды в одном месте
    дешевле, чем помнить два соглашения."""
    try:
        with open(CONFIG) as f:
            c = json.load(f)
    except FileNotFoundError:
        raise BusError(f"нет кредов шины: {CONFIG} — разверни плейбук nats")
    except ValueError as e:
        raise BusError(f"{CONFIG} нечитаем: {e}")
    if not c.get("url"):
        raise BusError(f"в {CONFIG} нет url")
    return c


# ─── соединение ──────────────────────────────────────────────────────────
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
    """Отказ прав NATS приезжает СЮДА, а не в ответ на запрос: сервер молча
    не доставляет публикацию, и запрос честно висит до таймаута. Без этого
    «нет права писать в mop.node.X.rpc» читалось бы как «агент молчит 20с» —
    диагноз, ведущий чинить работающий узел."""
    global _last_error
    _last_error = str(e)


async def _aconnect():
    c = config()
    return await nats.connect(
        servers=[c["url"]],
        user=c.get("user"), password=c.get("password"),
        tls_hostname=c.get("tls_hostname"),
        name="mop", error_cb=_on_error,
        # Молча копить неотправленное в ожидании сервера — худший вид отказа:
        # вызывающий получит успех, которого не было.
        allow_reconnect=True, max_reconnect_attempts=-1,
        reconnect_time_wait=2, connect_timeout=5,
    )


def connect():
    """Соединение процесса. Ленивое: фронтенды, которым шина не нужна
    (`mop add`, `mop delete`), не должны падать от её недоступности."""
    global _conn
    with _lock:
        if _conn is not None and not _conn.is_closed:
            return _conn
        try:
            _conn = _call(_aconnect(), 10)
        except BusError:
            raise
        except Exception as e:
            raise BusError(f"нет связи с шиной: {e}")
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
def subject(node, channel="rpc", shard=None):
    return f"mop.{shard or SHARD}.node.{node}.{channel}"


def broadcast(shard=None):
    """Все агенты разом. Нужен там, где спрашивающий не знает состава пула."""
    return f"mop.{shard or SHARD}.all.msg"


def inbox(master_id, shard=None):
    """Адрес мастера: сюда ему пишут агенты узлов И слейвы шарда.

    Обратный канал не заводил себе отдельного субъекта намеренно: у мастера
    уже есть ровно одно место, куда ему кладут вести, и права на него у
    слейва уже были (`mop.<шард>.master.>` в его creds). Отдельный субъект
    означал бы второй ответ на вопрос «куда писать мастеру» — и правку прав
    на сервере ради того, что и так разрешено.

    master_id=ALL_MASTERS — не адрес, а опрос: отвечают все живые мастера
    шарда. Так слейв узнаёт, кому он может ответить, не имея ростера."""
    return f"mop.{shard or SHARD}.master.{master_id}.inbox"


def events(shard=None):
    return f"mop.{shard or SHARD}.events"


# ─── запросы ─────────────────────────────────────────────────────────────


def _silence(who, timeout):
    """Почему тихо. Отличать «нет прав» от «агент лёг» обязательно: лечение
    у них разное и противоположное по стоимости ошибки."""
    if _last_error and "permissions violation" in _last_error.lower():
        return f"шина не пропустила запрос — {who}: {_last_error}"
    return f"{who} молчит {timeout}с"


def _ask(subj, who, dead, verb, timeout, **fields):
    """Запрос-ответ в субъект. -> разобранный ответ (dict).

    Кто на том конце — знает только вызывающий, поэтому имя адресата (`who`) и
    смысл его молчания (`dead`) приезжают параметрами. Разница не косметическая:
    «юнит mop-agent не работает» и «мастер закрыл сессию» лечатся по-разному, а
    единственное, что их различает, — субъект, в который спрашивали."""
    payload = json.dumps({"verb": verb, **fields}, ensure_ascii=False).encode()
    if len(payload) > MAX_PAYLOAD:
        raise BusError(f"запрос {verb} длиннее лимита шины "
                       f"({len(payload)} > {MAX_PAYLOAD} байт)")
    nc = connect()
    try:
        msg = _call(nc.request(subj, payload, timeout=timeout), timeout)
    except NoRespondersError:
        raise BusError(dead)
    except asyncio.TimeoutError:
        raise BusError(_silence(who, timeout))
    except Exception as e:
        raise BusError(f"{who}: {e}")
    try:
        return json.loads(msg.data.decode())
    except ValueError:
        raise BusError(f"{who} ответил не JSON: {msg.data[:120]!r}")


def request(node, verb, timeout=TIMEOUT, channel="rpc", shard=None, **fields):
    """Глагол агенту узла. -> разобранный ответ (dict).

    `shard` — явный параметр, а не поле запроса: адрес живёт в СУБЪЕКТЕ, и
    попади он в тело, права NATS его бы не увидели. Обычно не нужен: процесс
    ходит своим шардом, который ему поставил `mop master`.

    Ошибка агента приезжает полем `error` внутри ответа и НЕ поднимает
    исключение: это ответ, а не отказ шины. Исключение — только когда до
    агента не доехали."""
    return _ask(subject(node, channel, shard), f"агент узла {node}",
                f"агент узла {node} не подписан — юнит mop-agent не работает",
                verb, timeout, **fields)


def ask(master_id, verb, timeout=TIMEOUT, shard=None, **fields):
    """Глагол МАСТЕРУ, в его инбокс. -> разобранный ответ (dict).

    Обратная сторона канала: слейв отвечает тому, кто его послал. Запрос-ответ,
    а не публикация, ровно по той же причине, по какой её выбрали для `send`:
    отправителю нужен вердикт доставки, а не факт отправки. Публикация в инбокс
    мёртвого мастера выглядела бы успехом — а это ТИШИНА, то есть худший исход
    для петли, где отчёт слейва и есть главный сигнал."""
    return _ask(inbox(master_id, shard), f"мастер {master_id}",
                f"мастера {master_id} нет на шине: сессия закрыта или адрес не его "
                f"— посмотри mcp__mop__agents",
                verb, timeout, **fields)


def request_many(requests, timeout=TIMEOUT, channel="rpc", shard=None):
    """Разные запросы разным узлам, параллельно по одному соединению.

    Ради этого всё и затевалось: раньше состояние пула стоило по четыре
    рукопожатия exec'а на слейва ПОДРЯД. Запрос у каждого узла свой — он
    спрашивается о своих слейвах, — поэтому на входе {узел: {verb, **поля}},
    а не общий глагол.

    -> {узел: ответ | BusError}. Ошибка возвращается, а не бросается: один
    молчащий узел не должен уносить с собой картину по остальным."""
    if not requests:
        return {}
    nc = connect()

    async def one(node, req):
        try:
            msg = await nc.request(
                subject(node, channel, shard),
                json.dumps(req, ensure_ascii=False).encode(), timeout=timeout)
            return json.loads(msg.data.decode())
        except NoRespondersError:
            return BusError(f"агент узла {node} не подписан")
        except asyncio.TimeoutError:
            return BusError(_silence(f"агент узла {node}", timeout))
        except Exception as e:
            return BusError(f"{node}: {e}")

    nodes = list(requests)

    async def all_of():
        return await asyncio.gather(*(one(n, requests[n]) for n in nodes))

    return dict(zip(nodes, _call(all_of(), timeout)))


def gather(verb, timeout=5, subj=None, **fields):
    """Разослать глагол ВСЕМ агентам и собрать, кто отзовётся. -> [ответ].

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

    ОТВЕЧАЕМ, ЕСЛИ СПРАШИВАЮТ. Подписка на инбокс — это не только «мне
    положили весть»: тем же субъектом слейв шлёт мастеру ОТЧЁТ и ждёт вердикта
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
