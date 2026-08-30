"""Настройки этой установки: .env проекта поверх дефолтов в коде.

'Настройки делятся на три рода, и деление это не косметическое:

  ОБЯЗАТЕЛЬНЫЕ  осмысленного дефолта не имеют — адрес чужой локалки угадать
                нельзя. Нет в .env — mop отказывается работать и говорит,
                чего не хватает. Молча целиться в чужую сеть хуже отказа.
  С ДЕФОЛТОМ    порты, версии, датацентры: значение по умолчанию верно для
                любой установки, и переопределяют его редко.
  ПРОИЗВОДНЫЕ   дефолт СЧИТАЕТСЯ из других настроек. Задать руками можно, но
                обычно незачем: адрес API Nomad — это адрес сервера и порт
                API, и записывать его вторым местом значит завести источник
                правды, который однажды разойдётся с первым.
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
import pwd

PROJECT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV_FILE = os.path.join(PROJECT, ".env")

# Списки настроек — ОДИН источник на всю систему. Отсюда берут и библиотека, и
# `mop config`, и `mop deploy`, который отдаёт это плейбукам через
# --extra-vars. Плейбуки сами .env не читают: механизм передачи один, а на
# узлах .env вообще нет.

# Без этого mop не работает нигде, кроме той машины, где его писали.
#
# Она ОДНА, и это не случайно: всё остальное про кластер выводится из неё.
# Каждая настройка, которую можно вычислить, но которую заставляют вписать,
# — это ещё одно место, где установка расходится сама с собой.
REQUIRED = {
    "MOP_SERVER_LAN": "адрес сервера в локальной сети: туда ходят и узлы (RPC "
                      "Nomad, шина), и мастер (API Nomad)",
}

# Дефолт верен для любой установки; переопределяют редко.
DEFAULTS = {
    "MOP_HOME": os.path.expanduser("~"),
    "MOP_USER": os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name,
    "MOP_POOL_DC": "home",
    "MOP_CONTROL_DC": "control",
    "MOP_NOMAD_DATA": os.path.expanduser("~/nomad/data"),
    "MOP_NOMAD_VERSION": "1.10.5",
    # Откуда качать бинарь Nomad. Дефолт — официальные релизы; установке в
    # стране, откуда releases.hashicorp.com недоступен, нужно зеркало, и это
    # значение установки, а не литерал продукта.
    "MOP_NOMAD_MIRROR": "https://releases.hashicorp.com",
    "MOP_NATS_PORT": "4222",
    "MOP_NOMAD_PORT": "4646",       # HTTP API: туда ходит мастер
    "MOP_NOMAD_RPC_PORT": "4647",   # RPC: туда дозваниваются клиенты Nomad
    "MOP_NATS_VERSION": "2.14.6",
    "MOP_PUPPET_MEM_MB": "8192",
    # PATH папета на узлах. Версия node в nvm — свойство установки (какой
    # тулчейн стоит на узлах), а не продукта; {HOME} подставляется на месте.
    "MOP_PUPPET_PATH": "/usr/local/bin:/usr/bin:/bin:{HOME}/.local/bin:{HOME}/.cargo/bin:{HOME}/.nvm/versions/node/v22.12.0/bin",
    "MOP_FALLBACK_MODEL": "opus",
    # Каким профилем из mop/llm/ поднимать сессию без явного --llm: папета
    # или мастера. Выбор установки, а не продукта: контора на одном провайдере
    # меняет дефолт, а не каждую команду.
    "MOP_DEFAULT_LLM": "claude",
    # Что врапер подсеивает в клон из ~/puppet-env/<проект> — и ровно то же
    # глагол wipe щадит при git clean -x. Запятая, шаблоны gitignore-стиля;
    # второй список означал бы «посеяли одно, снесли другое» на первом же
    # рецикле.
    "MOP_PUPPET_SEED": ".env*,.providers",
    "MOP_SWEEP_FREE_MIN_GB": "60",
    "MOP_SWEEP_MAX_TARGET": "15GB",
    "MOP_SWEEP_STALE_DAYS": "14",
    # Жёсткий порог: ниже него mop gc пересоздаёт свободных папетов. Обязан
    # быть заметно ниже MOP_SWEEP_FREE_MIN_GB — сначала должно отработать
    # дешёвое подрезание target-ов, и только если оно не помогло, дорогой рецикл.
    "MOP_GC_FREE_MIN_GB": "25",
    # Сколько папетов mop gc готов пересоздать за один прогон: рецикл стоит
    # переклонирования зависимостей, чинить давление им надо не спеша.
    "MOP_GC_MAX_PER_RUN": "1",
    "MCP_PORT": "8000",
}

# Пусто = такой функциональности нет. Проверять надо ПУСТОТУ, а не отсутствие
# ключа: иначе выключенная функция и незаполненная настройка неразличимы.
OPTIONAL = {
    "MOP_RESERVED_MB": "",    # узел=МБ под чужих жильцов хоста
    # MCP автоматизации рабочего стола: пусто — папеты просто не увидят этих
    # серверов. Адреса именно IP, а не имена: по WINDOWS_MCP_HOST врапер ещё и
    # узнаёт, что папет живёт ВНУТРИ этого хоста, сравнивая его с адресами
    # своих интерфейсов.
    "WINDOWS_MCP_HOST": "",
    "MAC_MCP_HOST": "",
}

# Дефолт СЧИТАЕТСЯ, а не лежит литералом: он зависит от .env, которого на
# момент импорта ещё не читали. Значение из окружения или .env старше — разовое
# `NOMAD_ADDR=… mop list` обязано продолжать работать, и на внешний кластер
# нацеливаются им же.
DERIVED = {
    "NOMAD_ADDR": lambda: "http://{}:{}".format(get("MOP_SERVER_LAN"),
                                                get("MOP_NOMAD_PORT")),
}

SETTINGS = {**{k: "" for k in REQUIRED}, **{k: "" for k in DERIVED},
            **DEFAULTS, **OPTIONAL}


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
    правды: зашитый в точке вызова дефолт `NOMAD_ADDR` продолжал
    отдавать адрес автора там, где REQUIRED уже требовал заполнить его руками.
    Настройка описана в одном месте или ни в одном."""
    if default is None:
        default = SETTINGS.get(name, "")
    value = os.environ.get(name) or _load().get(name)
    if value:
        return value
    return DERIVED[name]() if not default and name in DERIVED else default


def effective():
    """Все настройки с учётом .env и окружения. -> {ИМЯ: (значение, откуда)}."""
    out = {}
    for name, default in SETTINGS.items():
        if os.environ.get(name):
            out[name] = (os.environ[name], "окружение")
        elif _load().get(name):
            out[name] = (_load()[name], ".env")
        elif name in DERIVED:
            out[name] = (DERIVED[name](), "вычислено")
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
