"""Креды сервера на машине оператора: ~/.config/mop/servers/<адрес>/.

Раньше их рендерил плейбук: bus.json и bus-master-<проект>.json с запечённым
адресом шины лежали на одной управляющей машине, и мастер вне неё не
поднимался, а второй сервер был невозможен в принципе — файлы одни. Мастер и
контроллер ansible были одной машиной, отсюда и схема.

Теперь мастер — любая машина с mop и входом на сервер: `MOP_SERVER_LAN`
из окружения (оно старше привязки клона) переключает адрес шины и каталог
кредов разом. Без привязки выбирается единственный joined-сервер. В каталоге лежит ровно то, что машине положено:

    operator.json               кто эта машина на шине: человек после
                                `mop join`, на сервере -- service (#104)
    client.json                 закрытые настройки прокси для этого сервера
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
# писалась двумя записями, и отказ между ними смешивал несвязанные входы.
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


def joined_host():
    """Единственный joined-сервер клиента; несколько требуют явного выбора.

    Joined -- след входа: целый профиль (#533: connection.json или целая
    legacy-пара), машинный вход или остаток половины как подсказка адреса
    для повторного login. Профиль из остатка не собирается: запускатели
    откажут сами, внятно."""
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


def _client_values(https_port, proxy_url, proxy_key):
    """Запись клиента: один порт TLS для шины, адрес и ключ LLM-прокси."""
    port = str(https_port)
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
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
    """Разбор connection.json без fallback. ValueError называет ремонт и не
    несёт секретов: содержимое файла в текст отказа не попадает."""
    try:
        with open(os.path.join(directory, CONNECTION_FILE), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read the connection profile: re-run mop login "
                         f"on {os.path.basename(directory)}") from e
    if not isinstance(raw, dict) or raw.get("version") != CONNECTION_VERSION:
        raise ValueError("unknown connection profile version: re-run mop login")
    try:
        bus_part, proxy_part = raw["bus"], raw["proxy"]
        values = _connection_values(bus_part["user"], bus_part["password"],
                                     raw["https_port"], proxy_part["url"],
                                     proxy_part["key"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid connection profile: re-run mop login") from None
    # Плоская форма -- та же, что у прежней пары: читатели (server_config,
    # session_env, models, doctor) не знают про схему файла.
    return {"user": bus_part["user"], "password": bus_part["password"],
            "https_port": values["https_port"], "proxy_url": values["proxy"]["url"],
            "proxy_key": values["proxy"]["key"]}


def human_profile(directory):
    """Профиль человека из каталога сервера, либо None. Один решатель для
    всех читателей (#533): connection.json, иначе ЦЕЛАЯ legacy-пара, ничего
    больше. Половина пары -- отказ, а не вход вполовину; machine operator
    рядом с профилем человека -- конфликт без выбора стороны."""
    conn = _read_connection(directory)
    op = operator(directory)
    if op is not None and busnames.is_machine(op["user"]):
        if conn is not None:
            raise ValueError("machine service identity and a human login conflict: "
                             "use a separate operator account or connection directory")
        return None     # служебный каталог: человеческого профиля в нём нет
    if conn is not None:
        return conn
    if op is None:
        try:
            got = client(directory)
        except ValueError as e:
            # Битый client.json -- отказ и без operator.json, с ремонтом.
            raise ValueError(f"{e}: re-run mop login") from None
        if got is not None:
            # Прокс-половина прежней пары: не вход, а след входа. Пароль
            # неизвестен, доверять старому ключу без нового login нельзя.
            raise ValueError("incomplete login: re-run mop login --server "
                             f"{os.path.basename(directory)}")
        return None
    try:
        joined = client(directory)
    except ValueError as e:
        # Битая пара -- отказ, не «нет входа»: ошибки client() несут свой
        # смысл, ремонт называем здесь.
        raise ValueError(f"{e}: re-run mop login") from None
    if joined is None:
        # Половина прежней пары -- не вход: пароль без прокси-настроек
        # запускал бы сессию без модели (#533). Восстановление -- полный вход.
        raise ValueError(f"incomplete login: re-run mop login --server "
                         f"{os.path.basename(directory)}")
    return {"user": op["user"], "password": op["password"],
            "https_port": joined["https_port"], "proxy_url": joined["proxy_url"],
            "proxy_key": joined["proxy_key"]}


def _machine_login(directory):
    """Служебный вход каталога (machine operator.json), либо None."""
    op = operator(directory)
    return op if op is not None and busnames.is_machine(op["user"]) else None


def joined_dirs(root=None):
    """Каталоги серверов, где есть след входа: целый профиль, машинный вход
    или ОСТАТОК прежней пары (#533). Остаток -- только подсказка выбора
    адреса для повторного login (main/ensure): профелем он не становится,
    и запускатели без полного входа откажут.

    Сломанный каталог -- кандидат всё равно: «не могу разобрать» не значит
    «выбор вслепую»; явный --server назовёт его и получит внятный отказ."""
    root = root or ROOT
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        d = os.path.join(root, name)
        try:
            whole = human_profile(d) is not None or _machine_login(d) is not None
        except ValueError:
            whole = os.path.isfile(os.path.join(d, CONNECTION_FILE)) or \
                os.path.isfile(os.path.join(d, CLIENT_FILE)) or \
                os.path.isfile(os.path.join(d, OPERATOR_FILE))
        if whole or os.path.isfile(os.path.join(d, CLIENT_FILE)) or \
                os.path.isfile(os.path.join(d, OPERATOR_FILE)):
            out.append(name)
    return out


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
