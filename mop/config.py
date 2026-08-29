"""Настройки этой установки: .env проекта поверх дефолтов в коде.

.env лежит в .gitignore, поэтому он ПЕРЕОПРЕДЕЛЯЕТ, а не задаёт: у каждой
переменной дефолт равен сегодняшнему значению кластера, и свежий клон без .env
поднимается как есть. Обязательными в .env остаются одни секреты.

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

# Список настроек и их дефолтов — ОДИН на всю систему. Отсюда берут и
# библиотека, и `mop config`, и `mop deploy`, который отдаёт это плейбукам
# через --extra-vars. Плейбуки сами .env не читают: механизм передачи один и
# тот же, каким туда уже едет список шардов.
SETTINGS = {
    # хост и пользователь на узлах пула
    "MOP_HOME": "/home/ermak",
    "MOP_USER": "ermak",
    "MOP_UID": "1000",
    # кластер Nomad
    "NOMAD_ADDR": "https://nomad.ermak.dev",
    "MOP_SERVER_LAN": "192.168.1.66",
    "MOP_POOL_DC": "home",
    "MOP_CONTROL_DC": "control",
    "MOP_NOMAD_DATA": "/home/ermak/nomad/data",
    "MOP_ACME_WEBROOT": "/home/ermak/ermak.dev",
    "MOP_NOMAD_VERSION": "1.10.5",
    # шина
    "MOP_NATS_PORT": "4222",
    "MOP_TLS_HOST": "nomad.ermak.dev",
    "MOP_NATS_VERSION": "2.14.6",
    # узлы
    "MOP_SSH_ALIAS": "gamer=gamer-wsl",
    "MOP_RESERVED_MB": "mirror=33602",
    # MCP автоматизации рабочего стола
    "MCP_PORT": "8000",
    "WINDOWS_MCP_HOST": "192.168.1.151",
    "WINDOWS_MCP_FQDN": "gamer.corp.ermak.dev",
    "MAC_MCP_HOST": "mac",
    # политика пула
    "MOP_SLAVE_MEM_MB": "8192",
    "MOP_FALLBACK_MODEL": "opus",
    # сторож диска
    "MOP_SWEEP_FREE_MIN_GB": "60",
    "MOP_SWEEP_MAX_TARGET": "15GB",
    "MOP_SWEEP_STALE_DAYS": "14",
}

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


def get(name, default):
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


def num(name, default):
    try:
        return int(get(name, default))
    except ValueError:
        return int(default)


def pairs(name, default):
    """`a=b,c=d` -> {"a": "b", "c": "d"}.

    Отображения (узел -> ssh-алиас, узел -> резерв памяти) в .env иначе не
    выразить, а заводить рядом второй формат файла — хуже, чем одна строка."""
    out = {}
    for chunk in get(name, default).split(","):
        k, sep, v = chunk.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out
