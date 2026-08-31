"""Реестр LLM-профилей: один файл в пакете — один профиль.

Профиль меняет ровно одно — КУДА папет ходит за токенами: эндпоинт
провайдера, карту моделей и имя ключа. Всё остальное (tmux, состояние,
детект залипаний) от профиля не зависит: врапер на узле получает уже
собранные PU_LLM_ENV/PU_LLM_KEY_VAR/PU_LLM_AUTH_VAR и самого профиля не
знает. Поэтому граница плагина проходит здесь, у мастера, и слой узлов
о расширении реестра не узнаёт.

Файл в этом каталоге — плагин, имя файла = имя профиля. Так же устроены
командлеты в bin/: обнаружение списком каталога, таблицы регистрации нет.
Контракт модуля:

    KEY       имя переменной в .env проекта либо None, если ключ не нужен.
              Сам ключ в спеку джоба НЕ кладём: она видна в UI Nomad и
              остаётся в её состоянии. На узлы ключ уезжает файлом
              secrets.env, и только названное явно.
    AUTH_VAR  куда врапер подставит ключ (дефолт ANTHROPIC_AUTH_TOKEN)
    ENV       статические переменные сессии папета, dict[str, str]

Контракт проверяется громко и с именем файла: KEY уезжает в sed-шаблон
врапера на узле, и кривое имя там молча совпадёт нигде — папет умрёт с
«нет ключа» далеко от причины. Дешёвое место поймать это — загрузка
реестра у мастера.
"""
import importlib
import re
from pathlib import Path

_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_AUTH_VAR = "ANTHROPIC_AUTH_TOKEN"

_CACHE = None


def contract(name, mod):
    """Модуль-плагин -> {key, auth_var, env, doc}; RuntimeError при нарушении.

    Отдельная от загрузки функция, потому что проверяема без пула
    (tests/llm.py): ошибка контракта обязана находиться до живых папетов.
    """
    where = f"mop/llm/{name}.py"
    env = getattr(mod, "ENV", None)
    key = getattr(mod, "KEY", None)
    auth_var = getattr(mod, "AUTH_VAR", DEFAULT_AUTH_VAR)
    doc = (mod.__doc__ or "").strip().splitlines()
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise RuntimeError(f"{where}: ENV must be dict[str, str]")
    if key is not None and (not isinstance(key, str) or not _VAR.match(key)):
        raise RuntimeError(f"{where}: KEY — .env variable name or None")
    if not isinstance(auth_var, str) or not _VAR.match(auth_var):
        raise RuntimeError(f"{where}: AUTH_VAR — environment variable name")
    return {"key": key, "auth_var": auth_var, "env": env,
            "doc": doc[0].strip() if doc else ""}


def profiles():
    """Весь реестр: {имя профиля: контракт}. Имя файла = имя профиля."""
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        for path in sorted(Path(__file__).parent.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            mod = importlib.import_module(f".{path.stem}", __package__)
            _CACHE[path.stem] = contract(path.stem, mod)
    return _CACHE


def get(name):
    """Профиль по имени либо None."""
    return profiles().get(name)


def require(name):
    """Профиль по имени; громкий отказ с перечнем доступных, если нет."""
    prof = get(name)
    if prof is None:
        raise RuntimeError(f"no LLM profile {name or '(empty)'}; available: "
                           f"{', '.join(profiles())} (mop llm)")
    return prof
