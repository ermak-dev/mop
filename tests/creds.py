#!/usr/bin/env python3
"""Креды сервера на машине оператора без пула: python3 tests/creds.py

Проверяется то, что раньше делал шаблон ansible и что теперь собирает сам
mop: конфиг шины из адреса, порта, проекта и пароля, имена файлов в каталоге
сервера и чтение токена. Ошибка тут молчит особенно охотно: не тот
пользователь NATS — это не отказ, а «агент не отвечает» через двадцать
секунд таймаута.

HYPOTHESIS (#51): креды мастера запечены плейбуком в bus-master-*.json одной
управляющей машины, и мастер вне неё либо со вторым сервером не поднимается.
SOLUTION: mop/common/creds.py собирает конфиг на лету из ~/.config/mop/servers/<адрес>/.
STATUS: FIXED — see #51
"""
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import creds  # noqa: E402

# (проект, пользователь NATS, файл пароля). Имена файлов совпадают с тем, что
# lookup('password') заводит в secrets/ сервера: копия без переименования.
USERS = [
    (None, "admin", "nats-admin.pass"),
    ("admin", "admin", "nats-admin.pass"),
    ("rugent", "master-rugent", "nats-master-rugent.pass"),
]


def main():
    bad = 0
    cases = 0

    for project, user, fname in USERS:
        cases += 1
        got = creds.bus_config("10.0.0.5", "4222", project, "s3cret")
        want = {"url": "nats://10.0.0.5:4222", "user": user, "password": "s3cret"}
        if got != want:
            bad += 1
            print(f"FAILED  bus_config({project!r}) -> {got}, wanted {want}")
        cases += 1
        if creds.pass_file(project) != fname:
            bad += 1
            print(f"FAILED  pass_file({project!r}) -> {creds.pass_file(project)!r}, "
                  f"wanted {fname!r}")

    # Кред папета: его выдаёт сервер в ответе на bootstrap песочницы (#83),
    # поэтому имя пользователя приходится называть явно — user_of() отвечает
    # про мастера, и молча получить master-<проект> там, где нужен
    # puppet-<проект>, значит выдать папету права мастера.
    cases += 1
    if creds.puppet_pass_file("rugent") != "nats-puppet-rugent.pass":
        bad += 1
        print(f"FAILED  puppet_pass_file -> {creds.puppet_pass_file('rugent')!r}")
    cases += 1
    got = creds.bus_config("10.0.0.5", "4222", "rugent", "s3cret",
                           user=creds.puppet_user("rugent"))
    want = {"url": "nats://10.0.0.5:4222", "user": "puppet-rugent",
            "password": "s3cret"}
    if got != want:
        bad += 1
        print(f"FAILED  bus_config with an explicit user -> {got}")
    cases += 1
    # Без явного пользователя поведение прежнее: проект -> мастер проекта.
    if creds.bus_config("10.0.0.5", "4222", "rugent", "x")["user"] != "master-rugent":
        bad += 1
        print("FAILED  bus_config without a user must stay the master of the project")

    # Каталог сервера — по адресу, и только по нему: два сервера — два
    # каталога, и переменная окружения переключает оба вместе с NOMAD_ADDR.
    cases += 1
    if creds.server_dir("10.0.0.5") != os.path.join(creds.ROOT, "10.0.0.5"):
        bad += 1
        print(f"FAILED  server_dir must key by address: {creds.server_dir('10.0.0.5')!r}")

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "nats-master-rugent.pass"), "w") as f:
        f.write("pw-with-newline\n")   # lookup('password') пишет с переводом строки
    with open(os.path.join(d, "bootstrap.json"), "w") as f:
        json.dump({"AccessorID": "x", "SecretID": "tok-123"}, f)

    cases += 1
    if creds.password(d, "rugent") != "pw-with-newline":
        bad += 1
        print(f"FAILED  password must be the file stripped: {creds.password(d, 'rugent')!r}")
    cases += 1
    if creds.password(d, "nobody") is not None:
        bad += 1
        print("FAILED  a missing password is None, not an exception: the caller "
              "has a legacy file to try next")
    cases += 1
    if creds.token(d) != "tok-123":
        bad += 1
        print(f"FAILED  token must be the bootstrap's SecretID: {creds.token(d)!r}")
    cases += 1
    if creds.token(os.path.join(d, "missing")) is not None:
        bad += 1
        print("FAILED  a missing token is None: the profile of mop mcp hangs on it")

    # Что едет мастеру из secrets/ сервера: только своё. Пароли узлов и
    # папетов мастеру не положены — с ними он мог бы представиться узлом.
    # HYPOTHESIS (#52): контроллер и `mop join` должны отбирать одни и те же
    # файлы, иначе на одной машине оператор увидит больше, чем на другой.
    # #106: общих паролей людей больше нет -- ни admin, ни master-<проект>.
    # Человек входит своим именем (`mop join --user`), и из secrets/ ему
    # положен только закреплённый сертификат.
    cases += 1
    listing = ["nats-node-mate.pass", "nats-master-rugent.pass", "junk",
               "nats-puppet-rugent.pass", "nats-admin.pass", "nats-master-mop.pass"]
    got = creds.pick(listing + [creds.CERT_FILE])
    want = [creds.CERT_FILE]
    if got != want:
        bad += 1
        print(f"FAILED  pick -> {got}, wanted {want}")

    # Пароль сервисов сервера оператору НЕ положен (#104): это машинный
    # пользователь, и отдав его человеку, отзыв доступа человека снова
    # означал бы смену пароля сервера.
    cases += 1
    if creds.SERVICE_PASS_FILE != "nats-service.pass" or \
            creds.SERVICE_PASS_FILE in creds.pick(listing + ["nats-service.pass"]):
        bad += 1
        print("FAILED  pick must never hand the service password to an operator")
    # STATUS: FIXED — see #104

    # Токен Nomad оператору НЕ положен (#82): управление уехало за шину, и
    # management-токен на каждой машине мастера — полный контроль над
    # кластером в обход всякой проверки проекта. Отбор — единственное место,
    # где это записано, поэтому проверяется отдельной строкой.
    cases += 1
    if creds.TOKEN_FILE in creds.pick(listing + [creds.TOKEN_FILE]):
        bad += 1
        print("FAILED  pick must never hand the Nomad token to an operator")

    # Контроллер собирает свой каталог сервера из secrets/ и bootstrap.json:
    # он тоже машина оператора, и после deploy на нём всё работает без join.
    secrets = tempfile.mkdtemp()
    for name in listing + [creds.CERT_FILE]:
        with open(os.path.join(secrets, name), "w") as f:
            f.write(name + "\n")
    # Токен в каталог кладёт fetch игры сервера, с правами по umask; collect
    # обязан закрыть и его (#54): на свежем контроллере он лежал бы 0644.
    dest = os.path.join(tempfile.mkdtemp(), "10.0.0.5")
    os.makedirs(dest)
    with open(os.path.join(dest, "bootstrap.json"), "w") as f:
        json.dump({"SecretID": "tok-123"}, f)
    os.chmod(os.path.join(dest, "bootstrap.json"), 0o644)
    # Переход (#106): ролевые пароли, собранные до него, в каталоге лежат.
    # Шина их больше не знает, и оставленные они лишь выдают «кредов нет»
    # за «креды есть».
    for n in ("nats-admin.pass", "nats-master-mop.pass"):
        with open(os.path.join(dest, n), "w") as f:
            f.write("old\n")
    cases += 1
    copied = creds.collect(secrets, dest)
    if sorted(os.listdir(dest)) != sorted(want + ["bootstrap.json"]) or copied != want:
        bad += 1
        print(f"FAILED  collect -> {copied}, dir {sorted(os.listdir(dest))}")
    cases += 1
    if creds.token(dest) != "tok-123":
        bad += 1
        print("FAILED  collected files must read back through token()")
    cases += 1
    modes = {n: os.stat(os.path.join(dest, n)).st_mode & 0o777 for n in os.listdir(dest)}
    if (os.stat(dest).st_mode & 0o777) != 0o700 or any(m != 0o600 for m in modes.values()):
        bad += 1
        print(f"FAILED  the server directory holds secrets: dir "
              f"{oct(os.stat(dest).st_mode & 0o777)}, files {modes}")

    # ── #125: сервер и логин `mop join` из рабочей копии.
    # Сервер: явно > привязка клона > единственный нашедшийся среди серверов,
    # где уже есть вход > .env; иначе отказ. STATUS: FIXED — see #125
    for args, want in [
        (dict(explicit="a", bound="b", serving=["c"], default="d"), "a"),
        (dict(explicit=None, bound="b", serving=["c"], default="d"), "b"),
        (dict(explicit=None, bound=None, serving=["c"], default="d"), "c"),
        (dict(explicit=None, bound=None, serving=[], default="d"), "d"),
    ]:
        cases += 1
        try:
            got = creds.pick_server(**args)
        except (AttributeError, ValueError) as e:
            got = e
        if got != want:
            bad += 1
            print(f"FAILED  pick_server({args}) -> {got!r}, wanted {want!r}")
    for args in (dict(explicit=None, bound=None, serving=["c", "e"], default="d"),
                 dict(explicit=None, bound=None, serving=[], default="")):
        cases += 1
        try:
            creds.pick_server(**args)
            bad += 1
            print(f"FAILED  pick_server({args}) must refuse: ambiguous or nothing")
        except ValueError:
            pass
        except AttributeError:
            bad += 1
            print("FAILED  creds.pick_server is missing")
    # Логин: явно > записанный для этого сервера > $USER.
    for args, want in [(("ivan", "anton", "ermak"), "ivan"),
                       ((None, "anton", "ermak"), "anton"),
                       ((None, None, "ermak"), "ermak")]:
        cases += 1
        got = getattr(creds, "pick_login", lambda *a: None)(*args)
        if got != want:
            bad += 1
            print(f"FAILED  pick_login{args} -> {got!r}, wanted {want!r}")

    # ── #130: отказ подключения к шине называет свою причину.
    # HYPOTHESIS: пустой NoServersError подменялся «wrong password», а
    # настоящая ошибка (TLS: чужой сертификат по чужому адресу) терялась.
    # SOLUTION: причина -- из ошибок попыток. STATUS: FIXED — see #130
    tls = ("Cannot connect to host mop.corp.ermak.dev:443 ssl:True "
           "[SSLCertVerificationError: (1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
           "certificate verify failed: unable to get local issuer certificate')]")
    fn = getattr(creds, "connect_failure", None)
    cases += 1
    got = fn("ermak", tls, "mop.corp.ermak.dev", 443, "79.141.65.134",
             presented="SHA256:20:39", pinned="SHA256:AE:71") if fn else ""
    if not all(w in got for w in ("ermak", "mop.corp.ermak.dev", "79.141.65.134",
                                  "SHA256:20:39", "SHA256:AE:71", "resolves")) \
            or "password" in got:
        bad += 1
        print(f"FAILED  a TLS refusal must name the address and both certificates: {got!r}")
    cases += 1
    got = fn("ermak", "nats: 'Authorization Violation'", "h", 443, "10.0.0.1") if fn else ""
    if "password" not in got:
        bad += 1
        print(f"FAILED  an authorization refusal is about the password: {got!r}")
    cases += 1
    got = fn("ermak", "Cannot connect to host h:443 ssl:True [Connect call failed ('10.0.0.1', 443)]",
             "h", 443, "10.0.0.1") if fn else ""
    if "10.0.0.1" not in got or "password" in got:
        bad += 1
        print(f"FAILED  an unreachable server is named with its address: {got!r}")
    cases += 1
    got = fn("ermak", "", "h", 443, None) if fn else ""
    if "password" in got or "h" not in got:
        bad += 1
        print(f"FAILED  no known reason must not be read as a wrong password: {got!r}")

    # ── #97: клиенты каталога сервера — на шину по wss через TLS-прокси.
    # HYPOTHESIS: bus_config всегда собирал nats://<LAN>:4222 без TLS, и
    # пароль оператора уходил по LAN открытым текстом.
    # SOLUTION: wss_config — адрес прокси (#96) и закреплённый сертификат;
    # bus_config остаётся узлам и папетам, их кред едет файлом и ответом
    # bootstrap.
    cases += 1
    got = creds.wss_config("mop.example", "443", None, "s3cret", user="anton")
    want = {"url": "wss://mop.example:443/nats", "user": "anton",
            "password": "s3cret"}
    if got != want:
        bad += 1
        print(f"FAILED  wss_config -> {got}, wanted {want}")
    cases += 1
    got = creds.wss_config("mop.example", "8443", "rugent", "pw",
                           cafile="/x/tls.pem")
    want = {"url": "wss://mop.example:8443/nats", "user": "master-rugent",
            "password": "pw", "cafile": "/x/tls.pem"}
    if got != want:
        bad += 1
        print(f"FAILED  wss_config with a pinned certificate -> {got}")

    # TLS-контекст: nats:// его не получает (4222 без TLS, и контекст там —
    # отказ на рукопожатии), wss — всегда с проверкой имени и цепочки.
    cases += 1
    if creds.tls_context({"url": "nats://10.0.0.5:4222"}) is not None:
        bad += 1
        print("FAILED  tls_context for nats:// must be None: 4222 speaks no TLS")
    cases += 1
    ctx = creds.tls_context({"url": "wss://mop.example:443/nats"})
    if ctx is None or ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        bad += 1
        print(f"FAILED  tls_context for wss must verify name and chain: {ctx}")

    # Закреплённый самоподписанный сертификат — единственный, кому верим:
    # системное доверие рядом с ним — лишняя дорога для подмены.
    if shutil.which("openssl"):
        pem = os.path.join(tempfile.mkdtemp(), creds.CERT_FILE)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
                        "-subj", "/CN=mop.example", "-keyout", pem + ".key",
                        "-out", pem], check=True, capture_output=True)
        cases += 1
        ctx = creds.tls_context({"url": "wss://mop.example:443/nats",
                                 "cafile": pem})
        cas = ctx.get_ca_certs() if ctx else []
        if len(cas) != 1:
            bad += 1
            print(f"FAILED  a pinned certificate must be the only trust: {len(cas)} CAs")
    else:
        # Без openssl проверка молчала вовсе (#229): теперь это пропуск.
        hermetic.skip("the pinned certificate check", "no openssl on this machine")

    # Сертификат едет оператору вместе с паролями: без него wss на
    # самоподписанный не поднимется ни у join по ssh, ни у контроллера.
    cases += 1
    if creds.CERT_FILE not in creds.pick(listing + [creds.CERT_FILE]):
        bad += 1
        print("FAILED  pick must bring the pinned certificate")
    cases += 1
    d = tempfile.mkdtemp()
    if creds.cafile(d) is not None:
        bad += 1
        print("FAILED  cafile of a directory without a pin is None: system trust")
    open(os.path.join(d, creds.CERT_FILE), "w").close()
    cases += 1
    if creds.cafile(d) != os.path.join(d, creds.CERT_FILE):
        bad += 1
        print(f"FAILED  cafile -> {creds.cafile(d)!r}")

    # Установка перешла на настоящий сертификат (MOP_TLS_CERT): старый
    # самоподписанный в secrets/ остаётся лежать, и закреплённый у клиента
    # он отверг бы настоящий. collect с pin=False обязан его снять.
    secrets = tempfile.mkdtemp()
    for n in ("nats-admin.pass", creds.CERT_FILE):
        with open(os.path.join(secrets, n), "w") as f:
            f.write(n)
    dest = os.path.join(tempfile.mkdtemp(), "mop.example")
    cases += 1
    creds.collect(secrets, dest)
    if creds.cafile(dest) is None:
        bad += 1
        print("FAILED  collect must pin the self-signed certificate")
    cases += 1
    got = creds.collect(secrets, dest, pin=False)
    if creds.cafile(dest) is not None or got != []:
        bad += 1
        print(f"FAILED  collect(pin=False) must drop the pin: {got}, "
              f"dir {sorted(os.listdir(dest))}")

    # Отпечаток — то, что оператор сверяет глазами при первом входе.
    cases += 1
    got = creds.fingerprint(b"x")
    want = "SHA256:" + ":".join(
        f"{b:02X}" for b in hashlib.sha256(b"x").digest())
    if got != want:
        bad += 1
        print(f"FAILED  fingerprint -> {got}, wanted {want}")
    # STATUS: FIXED — see #97

    c, b = check_no_human_over_nats_219()
    cases, bad = cases + c, bad + b

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


def check_no_human_over_nats_219():
    """HYPOTHESIS (#219): людей больше нет в users.conf -- их пускает только
    callout, и только через WebSocket (#105). Путь на сервере, который входит
    человеком по голому nats://, после #219 получил бы отказ: deploy, mop
    list/doctor под root на контроллере, дашборд, первый mop join, сервисы.
    SOLUTION: проверено по коду -- человеком ходят только через wss
    (bus.server_config -> creds.wss_config); nats:// строится только папету
    (bus_config с user=puppet), узлу (bus.json) и машинам (callout,
    can_login с puppet или service). Переходный пароль контроллера
    (nats-op-*.pass) join больше не читает.
    STATUS: FIXED — see #219
    -> (случаев, провалов)."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    cases = bad = 0

    def check(what, ok, detail=""):
        nonlocal cases, bad
        cases += 1
        if not ok:
            bad += 1
            print(f"FAILED  {what}" + (f": {detail}" if detail else ""))

    # Каталог сервера оператора -- всегда wss.
    # Как настоящий: servers/<адрес>/ -- make_dir закрывает и родителя.
    d = os.path.join(tempfile.mkdtemp(prefix="mop-test-creds-219-"), "servers", "10.0.0.1")
    creds.write_operator(d, "anton", "pw")
    keep = os.environ.get("MOP_SERVER_DIR")
    os.environ["MOP_SERVER_DIR"] = d
    try:
        from mop.common import bus
        c = bus.server_config("10.0.0.1")
        check("a person's bus config (the server directory) is wss",
              c["url"].startswith("wss://") and c["user"] == "anton", c)
    except ImportError as e:
        hermetic.skip("the wss check of bus.server_config", str(e))
    finally:
        if keep is None:
            os.environ.pop("MOP_SERVER_DIR", None)
        else:
            os.environ["MOP_SERVER_DIR"] = keep

    sources = {}
    for base, _, files in os.walk(os.path.join(root, "mop")):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(base, f)
                sources[os.path.relpath(path, root)] = open(path).read()
    # nats:// строится одним bus_config -- только папету (и машинам -- файлом).
    for path, text in sources.items():
        for m in re.finditer(r"creds\.bus_config\((.*?)\)\s*$", text, re.S | re.M):
            call = m.group(1)
            check(f"{path}: bus_config (plain nats://) only for a puppet",
                  "puppet_user" in call, call.strip()[:120])
        for m in re.finditer(r"can_login\(([^)]*)", text):
            call = m.group(1)
            check(f"{path}: can_login (plain nats://) only for a puppet or the service",
                  "puppet_user" in call or "SERVICE" in call or path == "mop/common/bus.py", call[:120])
        literal = [l for l in text.splitlines() if 'f"nats://' in l and "#" not in l.split('f"nats://')[0]]
        for line in literal:
            check(f"{path}: a nats:// URL only for machines (bus_config, can_login, callout)",
                  path in ("mop/common/creds.py", "mop/common/bus.py", "mop/server/callout.py"), line.strip())
    join = sources.get(os.path.join("mop", "cli", "pool", "join.py"), "")
    check("mop join reads no transition password (secrets/nats-op-*.pass)",
          "nats-op" not in join and "pass_file(" not in join)
    return cases, bad


if __name__ == "__main__":
    sys.exit(main())
