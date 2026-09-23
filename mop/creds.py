"""Креды сервера на машине оператора: ~/.config/mop/servers/<адрес>/.

Раньше их рендерил плейбук: bus.json и bus-master-<проект>.json с запечённым
адресом шины лежали на одной управляющей машине, и мастер вне неё не
поднимался, а второй сервер был невозможен в принципе — файлы одни. Мастер и
контроллер ansible были одной машиной, отсюда и схема.

Теперь мастер — любая машина с mop и адресом сервера: `MOP_SERVER_LAN` из
окружения (оно старше .env) переключает NOMAD_ADDR, адрес шины и каталог
кредов разом. В каталоге лежит ровно то, что машине положено:

    operator.json               кто эта машина на шине: человек после
                                `mop join`, на сервере -- service (#104)
    tls.pem                     самоподписанный сертификат TLS-прокси (#97)
    bootstrap.json              management-токен Nomad, только на контроллере

Общих паролей людей (nats-admin.pass, nats-master-<проект>.pass) здесь больше
нет (#106): человек входит своим именем, и что ему можно, решает роль в
MOP_OPERATORS, а не набор файлов.

Паролей узлов и папетов здесь нет намеренно: с ними мастер мог бы
представиться узлом, а границу проектов держат ровно креды.

Конфиг шины собирается из адреса, порта, проекта и пароля — чистая функция,
проверяется в tests/creds.py.
"""
import hashlib
import json
import os
import ssl

from . import config

ROOT = os.path.expanduser("~/.config/mop/servers")
# Кред оператора-человека (#84): один файл на машину, а не по паролю на
# проект. Пользователь тут один — сам человек, — и от проекта он не зависит:
# проект живёт в СУБЪЕКТЕ, права на субъект проверяет сервер NATS.
OPERATOR_FILE = "operator.json"
ADMIN = "admin"          # псевдопроект оператора: проекта нет
# Машинный пользователь сервисов сервера (#104): mop-web, mop-bootstrap,
# mop-cluster. Раньше они ходили под admin, и его пароль был общим с людьми.
# Лежит у сервера как operator.json -- «кто эта машина на шине», -- а
# оператору не отдаётся никогда (pick).
SERVICE = "service"
SERVICE_PASS_FILE = "nats-service.pass"
TOKEN_FILE = "bootstrap.json"
# Сертификат TLS-прокси сервера, закреплённый у клиента (#97). Только
# самоподписанный: настоящий проверяется системным доверием, и закреплять
# его значило бы сломать клиента на первом же продлении.
CERT_FILE = "tls.pem"
# Путь шины за прокси: nginx отдаёт его WebSocket-листенеру nats-server (#96).
WSS_PATH = "/nats"


def server_dir(host=None):
    """Каталог кредов сервера. Ключ — адрес, и только он: у двух серверов два
    каталога, и переменная окружения переключает их вместе с NOMAD_ADDR."""
    # MOP_SERVER_DIR -- каталог названный прямо (#123): сборщик работает под
    # пользователем контроллера, а на шину ходит как `service` из каталога
    # пользователя пула, а не как человек, чьи креды лежат у контроллера.
    return os.environ.get("MOP_SERVER_DIR") or os.path.join(
        ROOT, host or config.get("MOP_SERVER_LAN"))


def user_of(project):
    """Пользователь NATS по проекту: нет проекта — оператор."""
    return ADMIN if not project or project == ADMIN else f"master-{project}"


def pass_file(project):
    """Имя файла пароля — такое же, как в secrets/ сервера."""
    return f"nats-{user_of(project)}.pass"


def puppet_user(project):
    """Пользователь NATS папета. Отдельно от user_of: тот отвечает про
    мастера, и молча получить master-<проект> там, где нужен puppet-<проект>,
    значит выдать папету права мастера."""
    return f"puppet-{project}"


def puppet_pass_file(project):
    """Имя файла пароля папета — как в secrets/ сервера."""
    return f"nats-{puppet_user(project)}.pass"


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
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(ssl.DER_cert_to_PEM_cert(der))
    return path


def operator(directory):
    """{user, password} оператора этой машины, либо None.

    None — не отказ: пока установка не перевела операторов на собственные
    имена, рядом живёт прежний путь (пароль роли `master-<проект>`), и пустой
    файл означает просто «этот путь не выбран»."""
    try:
        with open(os.path.join(directory, OPERATOR_FILE)) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    if not got.get("user") or not got.get("password"):
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
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"user": user, "password": password}, f)
    return path


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
    os.makedirs(dest, mode=0o700, exist_ok=True)
    for d in (os.path.dirname(dest), dest):
        os.chmod(d, 0o700)


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
    оператора, и после `mop deploy` на нём всё должно работать без join.
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
