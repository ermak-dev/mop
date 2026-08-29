"""Настройки этой установки: .env проекта поверх дефолтов в коде.

'Настройки делятся на три рода, и деление это не косметическое:

  ОБЯЗАТЕЛЬНЫЕ  осмысленного дефолта не имеют — адрес чужого кластера угадать
                нельзя. Нет в .env — mop отказывается работать и говорит,
                чего не хватает. Молча целиться в чужую локалку хуже отказа.
  С ДЕФОЛТОМ    порты, версии, датацентры: значение по умолчанию верно для
                любой установки, и переопределяют его редко.
  НЕОБЯЗАТЕЛЬНЫЕ  пусто значит «такой функциональности нет»: MCP рабочего
                стола, ssh-алиасы, резерв памяти.

Порядок старшинства: переменная окружения > .env > дефолт. Окружение впереди,
потому что на нём уже стоят NOMAD_ADDR и NOMAD_TOKEN, и разовое
`NOMAD_ADDR=… mop list` должно продолжать работать.

ТОТ ЖЕ ФАЙЛ ЧИТАЕТ ANSIBLE (`lookup('ini', … type=properties)`), поэтому формат
намеренно примитивен: `КЛЮЧ=значение`, решётка — комментарий, кавычки по краям
снимаются. Ничего, чего не понял бы и sed.

НА УЗЛАХ ЭТОГО ФАЙЛА НЕТ: rsync его исключает, потому что там лежат креды
GitLab, которым в пуле делать нечего. Всё, что нужно узлу, приезжает туда
явно — секреты файлом `secrets.env`, настройки строками `Environment=` в юните
агента. Поэтому дефолты обязаны быть рабочими сами по себе.
"""
import os

PROJECT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV_FILE = os.path.join(PROJECT, ".env")

# Списки настроек — ОДИН источник на всю систему. Отсюда берут и библиотека, и
# `mop config`, и `mop deploy`, который отдаёт это плейбукам через
# --extra-vars. Плейбуки сами .env не читают: механизм передачи один, а на
# узлах .env вообще нет.

# Без этого mop не работает нигде, кроме той машины, где его писали.
REQUIRED = {
    "NOMAD_ADDR": "адрес API Nomad, например https://nomad.example.com",
    "MOP_SERVER_LAN": "адрес сервера в локальной сети: туда узлы дозваниваются "
                      "за RPC Nomad и за шиной",
    "MOP_TLS_HOST": "имя, на которое выписан сертификат шины",
}

# Дефолт верен для любой установки; переопределяют редко.
DEFAULTS = {
    "MOP_HOME": os.path.expanduser("~"),
    "MOP_USER": os.environ.get("USER") or "ermak",
    "MOP_UID": str(os.getuid()),
    "MOP_POOL_DC": "home",
    "MOP_CONTROL_DC": "control",
    "MOP_NOMAD_DATA": os.path.expanduser("~/nomad/data"),
    "MOP_NOMAD_VERSION": "1.10.5",
    "MOP_NATS_PORT": "4222",
    "MOP_NATS_VERSION": "2.14.6",
    "MOP_SLAVE_MEM_MB": "8192",
    "MOP_FALLBACK_MODEL": "opus",
    "MOP_SWEEP_FREE_MIN_GB": "60",
    "MOP_SWEEP_MAX_TARGET": "15GB",
    "MOP_SWEEP_STALE_DAYS": "14",
    "MCP_PORT": "8000",
}

# Пусто = такой функциональности нет. Проверять надо ПУСТОТУ, а не отсутствие
# ключа: иначе выключенная функция и незаполненная настройка неразличимы.
OPTIONAL = {
    "MOP_ACME_WEBROOT": "",   # где certbot берёт webroot; пусто — сертификат ваш
    "MOP_SSH_ALIAS": "",      # узел nomad=ssh-алиас; нужен только `mop attach`
    "MOP_RESERVED_MB": "",    # узел=МБ под чужих жильцов хоста
    "WINDOWS_MCP_HOST": "",   # MCP автоматизации рабочего стола: без них
    "WINDOWS_MCP_FQDN": "",   # слейвы просто не увидят этих серверов
    "MAC_MCP_HOST": "",
}

SETTINGS = {**{k: "" for k in REQUIRED}, **DEFAULTS, **OPTIONAL}


class Missing(RuntimeError):
    """Обязательная настройка не заполнена."""


def require():
    """Проверить обязательные. Зовут фронтенды перед работой с кластером.

    Отказ громкий и с перечнем: молча взять чужой дефолт и пойти в чужую
    локалку — худшее, что может сделать инструмент на новой машине."""
    gaps = [f"  {k} — {why}" for k, why in REQUIRED.items() if not get(k, "")]
    if gaps:
        raise Missing("не заполнены обязательные настройки в " + ENV_FILE
                      + ":\n" + "\n".join(gaps)
                      + "\n\nобразец: cp .env.example .env")

_env = None


def _load():
    global _env
    if _env is not None:
        return _env
    _env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, sep, v = line.partition("=")
                if sep:
                    _env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass  # нет .env — работаем на дефолтах, это штатный случай на узле
    return _env


def get(name, default=None):
    """Значение настройки: окружение > .env > дефолт из SETTINGS.

    Дефолт НЕ передаётся вызывающим. Пока передавался, каждая точка вызова
    несла свою копию — и копии пережили превращение SETTINGS в источник
    правды: `config.get("NOMAD_ADDR", "https://nomad.ermak.dev")` продолжал
    отдавать адрес автора там, где REQUIRED уже требовал заполнить его руками.
    Настройка описана в одном месте или ни в одном."""
    if default is None:
        default = SETTINGS.get(name, "")
    return os.environ.get(name) or _load().get(name) or default


def effective():
    """Все настройки с учётом .env и окружения. -> {ИМЯ: (значение, откуда)}."""
    out = {}
    for name, default in SETTINGS.items():
        if os.environ.get(name):
            out[name] = (os.environ[name], "окружение")
        elif _load().get(name):
            out[name] = (_load()[name], ".env")
        else:
            out[name] = (default, "дефолт")
    return out


def num(name, default=None):
    try:
        return int(get(name, default))
    except ValueError:
        return int(default if default is not None else SETTINGS[name])


def pairs(name, default=None):
    """`a=b,c=d` -> {"a": "b", "c": "d"}.

    Отображения (узел -> ssh-алиас, узел -> резерв памяти) в .env иначе не
    выразить, а заводить рядом второй формат файла — хуже, чем одна строка."""
    out = {}
    for chunk in get(name, default).split(","):
        k, sep, v = chunk.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out
