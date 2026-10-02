#!/usr/bin/env python3
"""Проверка окружения LLM без пула: python3 tests/llm.py

Профилей больше нет (#390): окружение сессии одно для папетов и мастеров --
адрес прокси из настройки, карта моделей, имя ключа. Всё -- чистые данные.
"""
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, context, creds, llm  # noqa: E402


# HYPOTHESIS: сессия и проверка прокси берут URL/ключ из .env, а без
# привязки сервер угадывается даже если join есть на двух установках.
# SOLUTION: выбранный сервер и его client.json — единственный источник для
# клиентских команд; без выбора при двух входах явный отказ.
# RESULT: два сервера разделены, один выбирается без подсказки, два отказывают.
# STATUS: FIXED — see #397
def check_joined_proxy_397(c):
    from mop.cli.core import _common
    root = os.path.join(tempfile.mkdtemp(prefix="mop-llm-397-"), "servers")
    with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN=None,
                                                MOP_PROXY_URL="http://wrong.test",
                                                MOP_PROXY_KEY="wrong-key"):
        for host, url, key, port in (("a.test", "https://a.test/llm", "a-key", "8443"),
                                     ("b.test", "https://b.test/llm", "b-key", "443")):
            creds.write_client(os.path.join(root, host), port, url, key)
        with config.client_sources():
            with context.use(context.resolve({"server": "a.test"}, {}, {})):
                result = _common.session_env()
                c.expect("#397 selected server supplies session URL", result["ANTHROPIC_BASE_URL"],
                         "https://a.test/llm")
                c.expect("#397 selected server supplies session key", result[llm.AUTH_VAR],
                         "a-key")
            with context.use(context.resolve({"server": "b.test"}, {}, {})):
                c.expect("#397 second server is independent", _common.session_env()[llm.AUTH_VAR],
                         "b-key")
            with context.use(context.resolve({}, {}, {})):
                try:
                    _common.session_env()
                    c.fail("#397 two joined servers require an explicit selection")
                except (RuntimeError, ValueError) as e:
                    c.check("#397 ambiguity identifies server choice", "server" in str(e).lower(),
                            str(e))
                try:
                    config.get("MOP_SERVER_LAN")
                    c.fail("#397 roster must not guess a server")
                except (RuntimeError, ValueError):
                    pass
        os.remove(os.path.join(root, "b.test", creds.CLIENT_FILE))
        with config.client_sources(), context.use(context.resolve({}, {}, {})):
            c.expect("#397 one joined server is the default", config.get("MOP_SERVER_LAN"),
                     "a.test")
            c.expect("#397 mop code can use the only server", _common.session_env()[llm.AUTH_VAR],
                     "a-key")


def check_session_ca_412(c):
    # HYPOTHESIS: session_env передаёт URL/ключ, но Node.js не получает
    # сохранённый сертификат; с ним та же сессия проходит TLS.
    # SOLUTION: для HTTPS передать tls.pem выбранного сервера через
    # NODE_EXTRA_CA_CERTS, не заменяя явный выбор пользователя.
    # RESULT: до исправления оба сервера давали None вместо пути CA.
    # STATUS: FIXED — see #412
    from mop.cli.core import _common
    with tempfile.TemporaryDirectory(prefix="mop-ca-412-") as root, \
            patched(creds, ROOT=root), patched_env(NODE_EXTRA_CA_CERTS=None), \
            config.client_sources():
        for host in ("a.test", "b.test"):
            directory = os.path.join(root, host)
            creds.write_client(directory, "443", f"https://{host}/llm", "key")
            cert = os.path.join(directory, creds.CERT_FILE)
            with open(cert, "w") as f:
                f.write("test certificate")
            with context.use(context.resolve({"server": host}, {}, {})):
                c.expect(f"#412 Claude trusts the selected server {host}",
                         _common.session_env().get("NODE_EXTRA_CA_CERTS"), cert)
        with context.use(context.resolve({"server": "a.test"}, {}, {})):
            with patched_env(NODE_EXTRA_CA_CERTS="/custom/ca.pem"):
                env = dict(os.environ, **_common.session_env())
                c.expect("#412 explicit user CA is preserved", env.get("NODE_EXTRA_CA_CERTS"),
                         "/custom/ca.pem")
            creds.write_client(os.path.join(root, "a.test"), "443", "http://a.test/llm", "key")
            c.check("#412 HTTP needs no extra CA", "NODE_EXTRA_CA_CERTS" not in _common.session_env())
            creds.write_client(os.path.join(root, "a.test"), "443", "https://a.test/llm", "key")
            os.remove(os.path.join(root, "a.test", creds.CERT_FILE))
            c.check("#412 no nonexistent CA path", "NODE_EXTRA_CA_CERTS" not in _common.session_env())


def main():
    c = Checks()
    check_joined_proxy_397(c)
    check_session_ca_412(c)
    # HYPOTHESIS (#390): реестр плагинов-профилей умер вместе с провайдерами:
    # держать его ради одного URL -- против KISS, а ENV профилей перестал
    # быть статикой плагина. SOLUTION: один модуль с env() и именем ключа;
    # адрес -- настройка установки, карта моделей -- константа до переезда
    # алиасов в прокси. STATUS: FIXED — see #390
    env = llm.env()
    c.check("the proxy URL comes from the setting (#390)",
            env["ANTHROPIC_BASE_URL"] == config.get("MOP_PROXY_URL")
            and bool(env["ANTHROPIC_BASE_URL"]), env.get("ANTHROPIC_BASE_URL"))
    c.check("the key rides under its own name (#390)",
            (llm.KEY, llm.AUTH_VAR), ("MOP_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN"))
    c.check("every model slot is named (#390)",
            all(env.get(k) for k in
                ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL")), env)
    c.check("long turns get a long client timeout (#382)",
            env.get("API_TIMEOUT_MS") == "3000000", env.get("API_TIMEOUT_MS"))
    # Спека и локальная сессия обязаны собирать окружение из одного места:
    # иначе врапер и мастер расходились бы молча.
    from mop.server import spec
    from mop.cli.core import _common
    with_normal = spec.task_env("pu-x-1", "git@h:g/x.git")
    baked = dict(kv.split("=", 1) for kv in
                 __import__("base64").b64decode(with_normal["PU_LLM_ENV"]).decode().splitlines())
    c.check("the spec bakes exactly llm.env() (#390)", baked, dict(llm.env()))
    c.check("the spec reads the key by llm.KEY (#390)",
            with_normal["PU_LLM_KEY_VAR"], llm.KEY)
    c.check("no profile names remain in the tree (#390)",
            not os.path.isdir(os.path.join(os.path.dirname(config.__file__), "llm")))
    # Клиентская сессия из joined-конфига проверяется выше (#397);
    # llm.env() остаётся серверным шаблоном для спеки папета.
    # HYPOTHESIS (#419): у сессий `mop master` и `mop code` инструмент
    # черновиков отзыва и команда /feedback живут своей жизнью: репозиторий
    # их не затыкал, и сессия отправляла черновики/команду от имени машины.
    # SOLUTION: session_env отдаёт и это -- настройкой feedbackDrafts=off
    # (узко: инструмент и очередь черновиков, не зонтик NONESSENTIAL_TRAFFIC,
    # который резал бы обновления плагинов) и переменную DISABLE_FEEDBACK_COMMAND
    # (команда /feedback). Оба значения -- код строки, контракт той же версии
    # claude, что и остальной спека-стенд.
    # RESULT: до исправления -- ни переменной, ни --settings в argv.
    # STATUS: FIXED — see #419
    from mop.cli.core import code, master
    want_args = ('--settings', '{"feedbackDrafts":"off"}')

    # Пара (флаг, значение) должна идти подряд: разрыв -- уже чужой аргумент.
    def carries(argv):
        return any(argv[i:i + 2] == list(want_args) for i in range(len(argv)))

    # session_env требует joined-сервер (#397) -- поднимем его в temp-каталоге,
    # как проверки #397/#412 выше.
    with tempfile.TemporaryDirectory(prefix="mop-419-") as root, \
            patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN=None), \
            config.client_sources():
        creds.write_client(os.path.join(root, "s.test"), "443",
                           "https://s.test/llm", "key")
        with context.use(context.resolve({}, {}, {})):
            session = _common.session_env()
    c.check("the session env carries the feedback kill switch (#419)",
            session.get("DISABLE_FEEDBACK_COMMAND") == "1", session)
    c.check("mop code passes feedbackDrafts=off to claude (#419)",
            carries(code.claude_argv(["--continue"])), code.claude_argv(["--continue"]))
    c.check("mop master passes feedbackDrafts=off to claude (#419)",
            carries(master.claude_argv([])), master.claude_argv([]))
    # HYPOTHESIS (правка #419, край пасстру): claude 2.1.287 не мержит
    # повторные --settings -- побеждает последний (проверено на живом
    # бинарнике: env из первого значения пропадает), а всё после `--`
    # парсер считает позиционными и флагом уже не читает. Значит append
    # нашего --settings В КОНЕЦ ломит обе края: чужие настройки он
    # затирает, а за `--` вообще не применяется. SOLUTION: вставлять пару
    # ДО пасстру (сразу за постоянными флагами) -- тогда наше значение
    # стоит раньше пользовательского. (Слияние внутрь его значения --
    # ниже, feedback_pair: требование «везде».) Хвост узнаём по
    # последнему элементу, а не по первому (флаг может совпасть с нашим).
    def carries_before_passthru(argv, passthru):
        # Пара подряд, и её конец -- до начала хвоста пасстру: хвост
        # узнаём по последнему элементу (уникальное значение), а не по
        # первому (флаг может совпасть с нашим).
        if not passthru:
            return any(argv[i:i + 2] == list(want_args) for i in range(len(argv)))
        tail = argv[-len(passthru):] if len(passthru) <= len(argv) else []
        if tail != passthru:
            return False
        head = argv[:-len(passthru)] if passthru else argv
        return any(head[i:i + 2] == list(want_args) for i in range(len(head)))

    user = ["--settings", '{"model":"opus"}']

    # Слияние пользовательского значения -- в feedback_settings (ниже);
    # командлетный слой проверяется с готовым memfd-путём.
    dash = ["--", "--settings", "custom.json"]
    c.check("mop code feedback settings stay effective before -- (#419)",
            carries(code.claude_argv(dash))
            and code.claude_argv(dash).index("--") > code.claude_argv(dash).index(want_args[0]),
            code.claude_argv(dash))
    c.check("mop master feedback settings stay effective before -- (#419)",
            carries(master.claude_argv(dash))
            and master.claude_argv(dash).index("--") > master.claude_argv(dash).index(want_args[0]),
            master.claude_argv(dash))
    # HYPOTHESIS (правка #419, требование «везде» + законный пасстру):
    # claude повторные --settings не мержит (последний побеждает целиком),
    # и наш off ДО пользовательского значения молча включал черновики,
    # когда у того ключа не было; отказ от его файла ломал пасстру
    # (hooks пользователя живут именно файлами). SOLUTION: последний
    # пользовательский --settings (файл или inline, до первого `--`,
    # форма через пробел и через «=») читается, feedbackDrafts=off
    # накладывается, итог едет ОДНИМ --settings memfd-путём -- без
    # argv-содержимого и без диска (подтверждено живым claude 2.1.287:
    # унаследованный /proc/self/fd/N читается). Свой --settings
    # отсутствует -- своя пара. Недоступный файл, не-JSON, не-объект --
    # ValueError (fail closed), без значения в тексте ошибки.
    # RESULT: слияние держит и off, и чужие env/hooks; края отказывают
    # явно и молча про значения.
    from mop.cli.core._common import feedback_fd, feedback_settings

    def merges(src, read=None):
        value, argv = feedback_settings(list(src), read=read)
        return value, argv, json.loads(value)

    files = {"custom.json": '{"env":{"X":"1"},"hooks":{"PreToolUse":[]}}'}
    read = files.__getitem__

    # Inline: off вписан, чужие env/hooks целы, своего --settings не осталось.
    value, argv, got = merges(["--settings", '{"env":{"X":"1"}}'])
    c.check("inline settings keep keys and gain off (#419)",
            got == {"env": {"X": "1"}, "feedbackDrafts": "off"}
            and "--settings" not in argv, argv)
    # Файл: прочитан, off наложен, путь из argv убран.
    value, argv, got = merges(["--settings", "custom.json"], read=read)
    c.check("settings file is read, merged, path removed (#419)",
            got == {"env": {"X": "1"}, "hooks": {"PreToolUse": []},
                    "feedbackDrafts": "off"}
            and "custom.json" not in argv, argv)
    # =-форма.
    value, argv, got = merges(["--settings=custom.json"], read=read)
    c.check("=form file merged the same way (#419)",
            got.get("feedbackDrafts") == "off" and "--settings" not in argv,
            argv)
    # Повторные --settings: ровно ОДНА пара в итоге -- merged с последнего;
    # ранние, останься они в пасстру, встали бы ПОСЛЕ нашей пары и вытеснили
    # off (claude повторов не мержит, побеждает последний). Все снимаются.
    value, argv, got = merges(["--settings", '{"env":{"A":"1"}}',
                              "--settings", '{"env":{"B":"2"}}'])
    c.check("repeated settings collapse into one merged pair (#419)",
            got == {"env": {"B": "2"}, "feedbackDrafts": "off"}
            and "--settings" not in argv
            and '{"env": {"A": "1"}}' not in argv, argv)
    # Дефект из ревью: пара не может встать в середину чужого флага с
    # значением. Точный argv: [claude, пара, *пасстру] -- при
    # пользовательском skip-флаге в середине набора.
    argv = code.claude_argv(["--model", "opus", "--dangerously-skip-permissions"],
                            "/proc/self/fd/3")
    c.check("pair never splits a user flag from its value (#419)",
            argv == ["claude", "--settings", "/proc/self/fd/3",
                     "--model", "opus", "--dangerously-skip-permissions"], argv)
    # `--settings` без значения (bare и пустое =) -- ясный отказ до
    # exec, не битый флаг: пустое иначе молча оставалось в пасстру ПОСЛЕ
    # нашей пары и вытесняло off. Inline-JSON с ведущим пробелом --
    # валидный для claude JSON, это не путь: мерджится как inline.
    for bad in (["--settings"], ["--continue", "--settings"],
                ["--settings="], ["--settings", ""]):
        try:
            feedback_settings(bad)
            c.check("dangling --settings must refuse (#419)", False, bad)
        except ValueError as e:
            c.check("dangling --settings must refuse (#419)",
                    "feedbackDrafts" in str(e), str(e))
    try:
        value, argv, got = merges(["--settings", ' {"env":{"X":"1"}}'])
        c.check("leading-space inline JSON merges as inline (#419)",
                got == {"env": {"X": "1"}, "feedbackDrafts": "off"}
                and "--settings" not in argv, argv)
    except ValueError as e:
        c.check("leading-space inline JSON merges as inline (#419)", False, str(e))
    # Своего --settings нет: своя пара, пасстру нетронут.
    value, argv, got = merges(["--continue"])
    c.check("no user settings -> own value, passthru intact (#419)",
            value == '{"feedbackDrafts": "off"}'.replace(": ", ":")
            or json.loads(value) == {"feedbackDrafts": "off"},
            (value, argv))
    # За `--` флагов нет: пользовательское значение за разделителем не
    # трогается, ours ставится командлетом в зоне флагов.
    value, argv, got = merges(["--", "--settings", '{"env":{"X":"1"}}'])
    c.check("beyond -- untouched, own value returned (#419)",
            json.loads(value) == {"feedbackDrafts": "off"}
            and argv == ["--", "--settings", '{"env":{"X":"1"}}'], argv)
    # Fail closed: недоступный файл, не-JSON, не-объект -- ValueError без
    # значения в тексте (секреты не эхом).
    def missing(path):
        raise FileNotFoundError(path)

    for bad, what, reader in (
            (["--settings", "custom.json"], "unreadable file", missing),
            (["--settings", "bad.json"], "not json",
             {"bad.json": "not json"}.__getitem__),
            (["--settings", "arr.json"], "not an object",
             {"arr.json": "[1,2]"}.__getitem__)):
        try:
            feedback_settings(bad, read=reader)
            c.check(f"{what} must refuse (#419)", False, bad)
        except ValueError as e:
            c.check(f"{what} must refuse, no value echoed (#419)",
                    "feedbackDrafts" in str(e) and "custom.json" not in str(e)
                    and "not json" not in str(e) and "[1,2]" not in str(e),
                    str(e))
    # memfd: путь вида /proc/self/fd/N, содержимое читается и равно значению.
    path, fd = feedback_fd(value)
    with open(path, encoding="utf-8") as f:
        c.check("feedback_fd carries the value unread via the fd path (#419)",
                path.startswith("/proc/self/fd/") and f.read() == value, path)
    os.close(fd)
    # Командлеты: один --settings в зоне флагов, до пасстру и до `--`,
    # пара не рвётся даже когда пользователь передал свой skip-флаг.
    argv = code.claude_argv(["--continue"], "/proc/self/fd/3")
    c.check("mop code: one settings flag before the passthru (#419)",
            argv[argv.index("--settings"):argv.index("--settings") + 2]
            == ["--settings", "/proc/self/fd/3"]
            and argv.index("--settings") < argv.index("--continue")
            and argv.count("--settings") == 1, argv)
    argv = master.claude_argv(["--", "--model", "opus"], "/proc/self/fd/3")
    c.check("mop master: settings flag before -- (#419)",
            argv.index("--settings") < argv.index("--")
            and argv.count("--settings") == 1, argv)
    argv = code.claude_argv(["--dangerously-skip-permissions", "--continue"],
                            "/proc/self/fd/3")
    c.check("pair intact next to the user skip flag (#419)",
            any(argv[i:i + 2] == ["--settings", "/proc/self/fd/3"]
                for i in range(len(argv))), argv)
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
