"""Креды сервера на машине оператора: ~/.config/mop/servers/<адрес>/.

Раньше их рендерил плейбук: bus.json и bus-master-<проект>.json с запечённым
адресом шины лежали на одной управляющей машине, и мастер вне неё не
поднимался, а второй сервер был невозможен в принципе — файлы одни. Мастер и
контроллер ansible были одной машиной, отсюда и схема.

Теперь мастер — любая машина с mop и входом на сервер: `MOP_SERVER_LAN`
из окружения (оно старше привязки клона) переключает адрес шины и каталог
кредов разом. Без привязки выбирается единственный joined-сервер. В каталоге лежит ровно то, что машине положено:

    connection.json             весь вход человека одной атомарной записью
                                (#533, #662 -- единственный клиентский
                                профиль)
    operator.json               машинный вход сервисов сервера (service,
                                #104); на машине человека не пишется
    client.json                 порт клиента для машинного входа сервисов;
                                пару с operator.json про человека решатель
                                больше не читает (#662)
    tls.pem                     самоподписанный сертификат TLS-прокси (#97)
    bootstrap.json              management-токен Nomad, только на контроллере

Общих паролей людей (nats-admin.pass, nats-master-<проект>.pass) здесь больше
нет (#106): человек входит своим именем, и что ему можно, решает роль в
провайдере личностей сервера (#219), а не набор файлов.

Паролей узлов и папетов здесь нет намеренно: с ними мастер мог бы
представиться узлом, а границу проектов держат ровно креды.

Конфиг шины собирается из адреса, порта, проекта и пароля — чистая функция,
проверяется в tests/creds.py.
"""
import hashlib
import json
import os
import ssl
from urllib.parse import urlsplit

from . import busnames, config, fsutil, paths  # noqa: F401 -- config: server_dir

ROOT = paths.local(paths.SERVERS)
# Кред оператора-человека (#84): один файл на машину, а не по паролю на
# проект. Пользователь тут один — сам человек, — и от проекта он не зависит:
# проект живёт в СУБЪЕКТЕ, права на субъект проверяет сервер NATS.
OPERATOR_FILE = "operator.json"
CLIENT_FILE = "client.json"
# Атомарный клиентский профиль (#533): весь вход человека одной записью --
# версия, креды шины, порт и прокси. Прежняя пара operator.json+client.json
# писалась двумя записями, и отказ между ними смешивал несвязанные входы;
# с #662 решатель её не читает -- это единственный клиентский профиль.
CONNECTION_FILE = "connection.json"
CONNECTION_VERSION = 1
ADMIN = busnames.ADMIN   # псевдопроект оператора: проекта нет
# Машинный пользователь сервисов сервера (#104): mop-web, mop-bootstrap,
# mop-cluster. Раньше они ходили под admin, и его пароль был общим с людьми.
# Лежит у сервера как operator.json -- «кто эта машина на шине», -- а
# оператору не отдаётся никогда (pick).
SERVICE = busnames.SERVICE
SERVICE_PASS_FILE = busnames.SERVICE_PASS_FILE
TOKEN_FILE = "bootstrap.json"
# Сертификат TLS-прокси сервера, закреплённый у клиента (#97). Только
# самоподписанный: настоящий проверяется системным доверием, и закреплять
# его значило бы сломать клиента на первом же продлении.
CERT_FILE = "tls.pem"
# Путь шины за прокси: nginx отдаёт его WebSocket-листенеру nats-server (#96).
WSS_PATH = "/nats"


def _runtime_grant_host():
    """Адрес выданного подключения роли: человеческий join в теле не нужен."""
    path = config.get("MOP_BUS_CONFIG")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            runtime = json.load(stream)
    except (OSError, ValueError):
        raise RuntimeError("cannot read the explicit runtime bus configuration") from None
    if not isinstance(runtime, dict):
        raise RuntimeError("the explicit runtime bus configuration is not a mapping")
    if not busnames.is_grant(runtime.get("user")):
        return None
    value = runtime.get("url")
    try:
        parsed = urlsplit(value)
        valid = (isinstance(value, str) and parsed.scheme in ("nats", "tls", "ws", "wss")
                 and parsed.hostname and parsed.port != 0
                 and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                 and parsed.path in ("", "/nats")
                 and not any(c.isspace() for c in value))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeError("the runtime role connection has no valid server address")
    return parsed.hostname


def joined_host():
    """Единственный joined-сервер клиента; несколько требуют явного выбора.

    Joined -- след входа: целый профиль (#533: connection.json) или
    машинный вход. Прежняя пара operator.json+client.json про человека
    больше не вход (#662): её каталог -- «нет входа», ремонт один --
    полный `mop login`. Явное подключение роли (#650) уже содержит
    сервер: его адрес не выбирается из человеческих каталогов."""
    runtime = _runtime_grant_host()
    if runtime:
        return runtime
    hosts = joined_dirs()
    if len(hosts) == 1:
        return hosts[0]
    if hosts:
        raise RuntimeError(f"multiple joined servers ({', '.join(hosts)}): "
                           f"name one with --server")
    raise RuntimeError("no joined server: run mop join --server <address>")


def server_dir(host=None):
    """Каталог кредов сервера. Ключ — адрес, и только он: у двух серверов два
    каталога, и переменная окружения переключает их вместе с NOMAD_ADDR."""
    # MOP_SERVER_DIR -- каталог названный прямо (#123): сборщик работает под
    # пользователем контроллера, а на шину ходит как `service` из каталога
    # пользователя пула, а не как человек, чьи креды лежат у контроллера.
    return config.get("MOP_SERVER_DIR") or os.path.join(
        ROOT, host or config.get("MOP_SERVER_LAN"))


def user_of(project):
    """Пользователь NATS по проекту: нет проекта — оператор."""
    return ADMIN if not project or project == ADMIN else busnames.master_user(project)


def pass_file(project):
    """Имя файла пароля — такое же, как в secrets/ сервера."""
    return busnames.pass_file(user_of(project))


def puppet_user(project):
    """Пользователь NATS папета. Отдельно от user_of: тот отвечает про
    мастера, и молча получить master-<проект> там, где нужен puppet-<проект>,
    значит выдать папету права мастера."""
    return busnames.puppet_user(project)


def puppet_pass_file(project):
    """Имя файла пароля папета — как в secrets/ сервера."""
    return busnames.pass_file(puppet_user(project))


def bus_config(host, port, project, password, user=None):
    """{url, user, password} — то, что раньше рендерил bus.json.j2.

    user называют явно там, где это не мастер: сервер выдаёт папету его кред
    в ответе на bootstrap песочницы (#83, docs/BOOTSTRAP.md)."""
    return {"url": f"nats://{host}:{port}", "user": user or user_of(project),
            "password": password}


def wss_config(host, https_port, project, password, user=None, cafile=None):
    """{url, user, password[, cafile]} клиента каталога сервера (#97): шина
    через TLS-прокси, а не сырой 4222. Пароль оператора не должен идти по
    сети открытым текстом -- тем более когда он станет паролем LDAP.

    bus_config остаётся узлам и папетам: их кред едет файлом и ответом
    bootstrap, генерируется и ничего, кроме шины, не открывает."""
    out = {"url": f"wss://{host}:{https_port}{WSS_PATH}",
           "user": user or user_of(project), "password": password}
    if cafile:
        out["cafile"] = cafile
    return out


def tls_context(c):
    """TLS-контекст для nats.connect по конфигу шины, либо None.

    nats:// -- None: на 4222 TLS нет, и контекст там -- отказ рукопожатия.
    wss -- всегда с проверкой имени и цепочки; закреплённый сертификат, если
    он есть, -- единственное доверие: системное рядом с ним было бы лишней
    дорогой для подмены."""
    if not c.get("url", "").startswith("wss://"):
        return None
    if c.get("cafile"):
        return ssl.create_default_context(cafile=c["cafile"])
    return ssl.create_default_context()


def cafile(directory):
    """Путь закреплённого сертификата в каталоге сервера, либо None --
    тогда доверие системное."""
    path = os.path.join(directory, CERT_FILE)
    return path if os.path.exists(path) else None


def fingerprint(der):
    """Отпечаток сертификата так, как его печатает openssl -fingerprint:
    его оператор сверяет глазами при первом входе."""
    return "SHA256:" + ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def untrusted_cert(host, port, timeout=10):
    """DER сертификата сервера, если система ему не доверяет; None -- если
    доверяет (настоящий сертификат закреплять нельзя: продление сломало бы
    клиента).

    Зовёт `mop join` ДО того, как отправит пароль: пароль уходит
    только в соединение, чей сертификат уже закреплён."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
                return None
    except ssl.SSLCertVerificationError:
        pass
    blind = ssl.create_default_context()
    blind.check_hostname = False
    blind.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port)), timeout) as raw:
        with blind.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def peer_cert(host, port, timeout=5):
    """DER сертификата, который сервер предъявляет сейчас, без проверки.
    Для отказа подключения (#130): доверяет ли ему система -- неважно, важно,
    тот ли он, что закреплён."""
    import socket
    blind = ssl.create_default_context()
    blind.check_hostname = False
    blind.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port)), timeout) as raw:
        with blind.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def connect_failure(user, error, host, port, addr, presented=None, pinned=None):
    """Отказ подключения к шине -> строка причины. Чистая функция (#130).

    error -- текст последней ошибки попытки (nats-py отдаёт её в error_cb, а
    само исключение -- пустой NoServersError). Пустая причина прежде читалась
    как «неверный пароль», и чужой сертификат по чужому адресу искали в
    паролях."""
    head = f"no connection to bus as {user}: "
    where = f"{host}:{port}" + (f" ({addr})" if addr else " (does not resolve)")
    text = error or ""
    if "CERTIFICATE_VERIFY_FAILED" in text or "SSLCertVerificationError" in text:
        return (head + f"{where} presented a certificate other than the one expected "
                f"(presented {presented or '?'}, pinned {pinned or 'none: the system CAs'}). "
                f"Check where {host} resolves: getent hosts {host}")
    if "Authorization Violation" in text or "authorization" in text.lower():
        return head + "wrong password, or the bus does not know this user any more"
    if not addr or "Connect call failed" in text or "Cannot connect" in text \
            or "timed out" in text.lower() or "Name or service not known" in text:
        return head + f"cannot reach {where}" + (f": {text}" if text else "")
    return head + (text or f"{where} refused without a reason")


def write_cert(directory, der):
    """Закрепить сертификат в каталоге сервера. -> путь."""
    make_dir(directory)
    path = os.path.join(directory, CERT_FILE)
    fsutil.write_private(path, ssl.DER_cert_to_PEM_cert(der))
    return path


def operator(directory):
    """{user, password} оператора этой машины, либо None.

    None — не отказ: пока установка не перевела операторов на собственные
    имена, рядом живёт прежний путь (пароль роли `master-<проект>`), и пустой
    файл означает просто «этот путь не выбран»."""
    got = fsutil.read_json(os.path.join(directory, OPERATOR_FILE))
    if got is None:
        return None
    # Валидный JSON не-объект или не строки -- тот же «путь не выбран» (#336).
    if not isinstance(got, dict) or not all(
            isinstance(got.get(k), str) and got[k] for k in ("user", "password")):
        return None
    return {"user": got["user"], "password": got["password"]}


def pick_server(explicit, bound, serving, default):
    """Сервер `mop join` (#125). Чистая функция.

    explicit -- аргумент или окружение; bound -- привязка клона; serving --
    серверы, где уже есть вход и чей реестр знает origin клона; default --
    .env. Два нашедшихся -- отказ, а не первый попавшийся: креды ушли бы не
    туда. Ничего -- отказ: адрес при первом входе называют явно."""
    if explicit:
        return explicit
    if bound:
        return bound
    if len(serving) > 1:
        raise ValueError(f"this project is served by {', '.join(sorted(serving))}: "
                         f"name one, mop join --server <address>")
    if serving:
        return serving[0]
    if default:
        return default
    raise ValueError("no server known for this working copy: "
                     "mop join --server <address>")


def pick_login(explicit, stored, env_user):
    """Логин `mop join`: явно > записанный для этого сервера > $USER."""
    return explicit or stored or env_user


def write_operator(directory, user, password):
    """Положить кред оператора. Каталог и файл закрыты: там пароль."""
    make_dir(directory)
    path = os.path.join(directory, OPERATOR_FILE)
    fsutil.write_private(path, json.dumps({"user": user, "password": password}))
    return path


def valid_https_port(text):
    """Правда ли это HTTPS-порт: ASCII-десятичная строка 1..65535 (#550).

    Единственный источник правила для всех, кто складывает порт в каталог
    сервера (client.json, connection.json, login): три копии условия уже
    были бы тремя границами. isdecimal без isascii пускал бы Unicode-цифры
    («٤٤٣»), знак и пробел -- не десятичная запись числа. Предикат:
    отказ с текстом СВОЕГО места поднимает вызывающий. Не строка -- не
    порт (False, не падение)."""
    if not isinstance(text, str):
        return False
    return text.isascii() and text.isdecimal() and 1 <= int(text) <= 65535


def _client_values(https_port, proxy_url, proxy_key):
    """Запись клиента: один порт TLS для шины, адрес и ключ LLM-прокси."""
    port = str(https_port)
    if not valid_https_port(port):
        raise ValueError("joined server HTTPS port must be between 1 and 65535")
    try:
        url = urlsplit(proxy_url)
        valid = (url.scheme in ("http", "https") and url.hostname and url.port != 0
                 and not (url.username or url.password or url.query or url.fragment))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid or any(c.isspace() for c in proxy_url):
        raise ValueError("joined proxy URL must be an HTTP(S) address without credentials")
    if not isinstance(proxy_key, str) or not proxy_key or any(
            c in "\r\n\x00" for c in proxy_key):
        raise ValueError("joined proxy key must be a nonempty single line")
    return {"https_port": port, "proxy_url": proxy_url, "proxy_key": proxy_key}


def client(directory):
    """Настройки прокси выбранного сервера, либо None до первого join.

    Сломанная запись -- отказ, не переход к .env или соседнему серверу."""
    try:
        with open(os.path.join(directory, CLIENT_FILE), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError("cannot read joined client config") from e
    if not isinstance(raw, dict) or set(raw) != {"https_port", "proxy_url", "proxy_key"}:
        raise ValueError("invalid joined client config")
    return _client_values(**raw)


def write_client(directory, https_port, proxy_url, proxy_key):
    """Сохранить всю проверенную запись атомарно, отдельно от пароля шины."""
    values = _client_values(https_port, proxy_url, proxy_key)
    make_dir(directory)
    path = os.path.join(directory, CLIENT_FILE)
    fsutil.write_private(path, json.dumps(values))
    return path


# ─── атомарный профиль человека (#533) ──────────────────────────────────
class ProfileError(ValueError):
    """Отказ решателя профиля с ТИПОМ причины (#533): читателю -- текст,
    login различает случаи по kind, а не поиском слов в сообщении.
    broken -- испорченный/неизвестный новый файл или битая запись пары:
    ремонта полным входом нет и перезапись запрещена (файл чинит человек);
    conflict -- machine operator рядом с человеческим профилем; partial --
    половина прежней пары: восстановима полным входом. Текст не советует
    `re-run mop login` там, где вход не ремонт (broken), и не несёт
    секретов."""

    def __init__(self, kind, text):
        super().__init__(text)
        self.kind = kind


def _operator_strict(directory):
    """operator.json для решателя: None -- файла нет; {user, password} --
    валидная запись; ProfileError("broken") -- есть, но не читается или не
    той формы. Публичный operator() молча даёт None для мусора -- для
    «кто эта машина» это верно, а решателю нужна правда: иначе битый
    operator.json превращался бы в «оператора нет», и recovery полного
    входа перезаписывал бы неизвестно чью запись (#533)."""
    path = os.path.join(directory, OPERATOR_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ProfileError("broken", f"cannot read the operator record in "
                          f"{directory}: {e.__class__.__name__}") from e
    if not isinstance(raw, dict) or set(raw) != {"user", "password"} or \
            not all(isinstance(raw[k], str) and raw[k] for k in ("user", "password")):
        raise ProfileError("broken", f"invalid operator record in {directory}")
    return {"user": raw["user"], "password": raw["password"]}


def _connection_values(user, password, https_port, proxy_url, proxy_key):
    """Схема connection.json целиком: одна запись -- один вход. Значения
    проходят проверку прежних _client_values: порт, URL и ключ -- те же
    правила, что и у пары."""
    if not isinstance(user, str) or not user or busnames.is_machine(user):
        # Машинное имя сюда не пишет и writer, но и читатель не примет:
        # connection.json -- профиль человека, служебный вход живёт в
        # operator.json и в него не переводится.
        raise ValueError("connection profile belongs to a person, not a machine login")
    if not isinstance(password, str) or not password:
        raise ValueError("connection profile needs a bus password")
    client = _client_values(https_port, proxy_url, proxy_key)
    return {"version": CONNECTION_VERSION, "bus": {"user": user, "password": password},
            "https_port": client["https_port"], "proxy": {"url": client["proxy_url"],
                                                          "key": client["proxy_key"]}}


def write_connection(directory, user, password, https_port, proxy_url, proxy_key):
    """Единственный атомарный writer профиля человека: одна запись после
    обеих сетевых проверок (#533). Каталог и файл закрыты, читатель видит
    либо прежний профиль целиком, либо новый целиком."""
    values = _connection_values(user, password, https_port, proxy_url, proxy_key)
    make_dir(directory)
    path = os.path.join(directory, CONNECTION_FILE)
    fsutil.write_private(path, json.dumps(values))
    return path


def _read_connection(directory):
    """Разбор connection.json без fallback. Схема version 1 -- замкнутая:
    неизвестные поля на верхнем уровне и в bus/proxy -- отказ, не потеря:
    relogin writer'ом молча выбросил бы чужие данные. Тексты отказов не
    советуют `re-run mop login` -- вход такой файл не ремонтирует -- и не
    несут секретов: содержимое файла в текст не попадает."""
    try:
        with open(os.path.join(directory, CONNECTION_FILE), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except OSError as e:
        # Права -- не порча: класс исключения безопасен и точен, «не JSON»
        # оклеветало бы чужие права как битый формат (#533).
        raise ProfileError("broken", f"cannot read the connection profile in "
                          f"{directory}: {e.__class__.__name__}") from e
    except ValueError as e:
        raise ProfileError("broken", f"cannot read the connection profile in "
                          f"{directory} ({e.__class__.__name__}): the file "
                          f"is not valid JSON") from e
    # Версия -- строго int: bool в Python равен единице, и `true` в файле
    # иначе прочитался бы как версия 1. Неизвестная версия -- отказ, не None:
    # молчаливое «нет входа» прятало бы чужой формат за повторным входом.
    # Числовой тип -- после проверки int, это факт формата; ПРОИЗВОЛЬНОЕ
    # значение (строка, токен, чужие данные) в текст не цитируется (#533).
    version = raw.get("version") if isinstance(raw, dict) else None
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProfileError("broken", f"unsupported connection profile in {directory}: "
                          f"the version is {type(version).__name__}, not a number")
    if version != CONNECTION_VERSION:
        raise ProfileError("broken", f"unsupported connection profile in {directory}: "
                          f"version {version} (supported: {CONNECTION_VERSION})")
    if not isinstance(raw, dict) or set(raw) != {"version", "bus", "https_port", "proxy"} \
            or not isinstance(raw["bus"], dict) or set(raw["bus"]) != {"user", "password"} \
            or not isinstance(raw["proxy"], dict) or set(raw["proxy"]) != {"url", "key"}:
        raise ProfileError("broken", f"invalid connection profile in {directory}: "
                          f"the record does not match version "
                          f"{CONNECTION_VERSION}")
    try:
        values = _connection_values(raw["bus"]["user"], raw["bus"]["password"],
                                     raw["https_port"], raw["proxy"]["url"],
                                     raw["proxy"]["key"])
    except (TypeError, ValueError):
        raise ProfileError("broken", f"invalid connection profile in {directory}: "
                          f"the record does not match version "
                          f"{CONNECTION_VERSION}") from None
    # Плоская форма -- та же, что у прежней пары: читатели (server_config,
    # session_env, models, doctor) не знают про схему файла.
    return {"user": raw["bus"]["user"], "password": raw["bus"]["password"],
            "https_port": values["https_port"], "proxy_url": values["proxy"]["url"],
            "proxy_key": values["proxy"]["key"]}


def snapshot(directory):
    """ОДИН снимок каталога сервера: каждый файл читается один раз (#533).
    -> ("human", профиль) | ("machine", {user, password, https_port|None}) |
    (None, None). Конфликт machine operator + человеческий профиль и битые
    записи -- ProfileError с типом причины.

    Единственный решатель для шины и login: два отдельных чтения увидели бы
    два поколения каталога, если writer заменил файл между ними. Каталог
    без целого connection.json -- не вход (#662: прежняя пара
    operator.json+client.json про человека больше не читается, половины --
    не подсказка): ремонт один, полный `mop login`."""
    conn = _read_connection(directory)
    op = _operator_strict(directory)
    if op is not None and busnames.is_machine(op["user"]):
        if conn is not None:
            raise ProfileError("conflict", "machine service identity and a human login "
                               "conflict: use a separate operator account "
                               "or connection directory")
        # Служебный каталог -- прежний контракт шины: порт из client.json,
        # если лежит (битый -- отказ, как раньше), иначе настройка; прокси
        # не обязателен.
        joined = client(directory)
        return "machine", {"user": op["user"], "password": op["password"],
                           "https_port": joined["https_port"] if joined else None}
    if conn is not None:
        return "human", conn
    return None, None


def human_profile(directory):
    """Профиль человека из каталога сервера, либо None. Тонкая обёртка
    snapshot (#533): пустой каталог, машинный и неполный вход профелем
    человека не становятся, остальные случаи решает общий снимок."""
    kind, who = snapshot(directory)
    return who if kind == "human" else None


def human_login(directory, host):
    """Полный человеческий профиль каталога или RuntimeError (#549).

    Единственный владелец требования «потребителю прокси нужен целый вход
    человека»: до этого тот же блок (snapshot -> ProfileError ->
    RuntimeError -> разбор kind) был повторён в session_env,
    models.available и doctor._probe тремя копиями -- и разошёлся бы при
    первой правке текста. Тексты прежние дословно: machine/partial/пусто
    -- разные ремонты (отдельная учётка, повторный вход, первый вход),
    ProfileError (битое, конфликт ролей) идёт дословно. Один снимок на
    операцию; прав, форматов и приоритетов решатель не меняет."""
    try:
        kind, record = snapshot(directory)
    except ProfileError as e:
        raise RuntimeError(str(e)) from e
    if kind == "human":
        return record
    if kind == "machine":
        # Служебный каталог: человеческий login в него сам запрещён, совет
        # «войди сюда» был бы заведомо неработающим. Отдельная учётка не
        # требует другой установки: вход на ТОМ ЖЕ сервере, в свой каталог.
        raise RuntimeError(f"no human login for {host} (a machine service "
                           f"directory): use a separate operator account "
                           f"or connection directory, run mop login "
                           f"--server {host} <login>")
    raise RuntimeError(f"no login for {host}: run mop login --server {host}")


def joined_dirs(root=None):
    """Каталоги серверов, где есть след входа: целый профиль или машинный
    вход (#533, #662). Остатков прежней пары больше нет: вход без целого
    connection.json -- не вход, его ремонт один -- полный `mop login`.

    След -- наличие файла, не содержимое: сломанный каталог остаётся
    кандидатом, «не могу разобрать» не значит «выбор вслепую»; явный
    --server назовёт его и получит внятный отказ."""
    root = root or ROOT
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    return [name for name in names
            if any(os.path.isfile(os.path.join(root, name, f))
                   for f in (CONNECTION_FILE, CLIENT_FILE, OPERATOR_FILE))]


def password(directory, project):
    """Пароль из каталога сервера, либо None: у вызывающего есть запасной
    путь (старые файлы плейбука), и пустой каталог — не отказ."""
    try:
        with open(os.path.join(directory, pass_file(project))) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def token(directory):
    """SecretID из bootstrap.json, либо None: на отсутствии токена держится
    профиль `mop mcp` (узел не управляет джобами), и исключение тут —
    неверный сигнал."""
    try:
        with open(os.path.join(directory, TOKEN_FILE)) as f:
            return json.load(f)["SecretID"]
    except (FileNotFoundError, ValueError, KeyError):
        return None


def make_dir(dest):
    """Каталог сервера и его родитель закрыты от всех: там пароли. mode у
    makedirs действует только на последний уровень, поэтому явно."""
    for d in (os.path.dirname(dest), dest):
        fsutil.make_private_dir(d)


def pick(listing):
    """Что из secrets/ сервера едет в каталог сервера оператора: только
    закреплённый сертификат. Паролей людям не копируют (#106) -- человек
    входит своим именем, и его пароль приходит `mop join`."""
    return sorted(n for n in listing if n == CERT_FILE)


def legacy(listing):
    """Ролевые пароли людей, оставшиеся от времён до #106. -> [имена].

    Шина их больше не знает, а лежащие они выдают «кредов нет» за «креды
    есть»: мастер поднялся бы, чтобы не подключиться."""
    return sorted(n for n in listing if n == pass_file(None)
                  or (n.startswith("nats-master-") and n.endswith(".pass")))


def collect(secrets_dir, dest, pin=True):
    """Собрать каталог сервера на самом контроллере: он тоже машина
    оператора, и после `mop server deploy` на нём всё должно работать без join.
    Токен сюда кладёт игра сервера (fetch), а тот пишет с правами по
    umask -- поэтому права закрываются у всего, что лежит в каталоге, а не
    только у скопированного. -> имена положенных файлов.

    pin=False -- у установки настоящий сертификат (MOP_TLS_CERT): старый
    самоподписанный остаётся лежать в secrets/, и закреплённый здесь он
    отверг бы настоящий. Поэтому его не копируют, а прежнюю копию снимают."""
    import shutil
    make_dir(dest)
    names = [n for n in pick(os.listdir(secrets_dir)) if pin or n != CERT_FILE]
    for n in legacy(os.listdir(dest)):
        os.remove(os.path.join(dest, n))
    if not pin and os.path.exists(os.path.join(dest, CERT_FILE)):
        os.remove(os.path.join(dest, CERT_FILE))
    for n in names:
        shutil.copyfile(os.path.join(secrets_dir, n), os.path.join(dest, n))
    for n in os.listdir(dest):
        os.chmod(os.path.join(dest, n), 0o600)
    return names
