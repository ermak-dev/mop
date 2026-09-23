"""Секреты проекта: файлы и переменные, которые папет получает при старте (#127).

Мастер кладёт их из рабочей копии (`mop secret file|var add`), сервер хранит,
bootstrap доставляет в тело при каждом старте папета, врапер кладёт файлы в
клон, а переменные — в окружение сессии. Раньше файл должен был лежать на
машине, которая играет bootstrap, а с тех пор как её играет сервер, свежий
`.env` с машины мастера туда доставить было нечем.

На сервере, под пользователем пула:

    ~/.config/mop/project-secrets/<проект>/files/<путь от корня клона>
    ~/.config/mop/project-secrets/<проект>/vars.env      KEY=VALUE построчно

Каталоги 0700, файлы 0600. Правит их сервис кластера глаголами на субъекте
проекта — мастеру своего проекта и оператору; узлам субъект не выдан.

Данные, без печати.
"""
import os
import posixpath
import re

ROOT = os.path.expanduser("~/.config/mop/project-secrets")
# Файл едет одним сообщением шины (max_payload 1 МБ) в base64.
MAX_BYTES = 512 * 1024
VARS = "vars.env"
FILES = "files"

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ─── чистое ──────────────────────────────────────────────────────────────
def valid_name(name):
    """Путь файла от корня клона, нормализованный. Выход за клон -- отказ:
    файл лёг бы мимо клона папета, а то и поверх чужого."""
    n = posixpath.normpath((name or "").strip())
    if not n or n == "." or n.startswith("/") or n == ".." or n.startswith("../"):
        raise ValueError(f"{name!r}: a secret file is a path inside the working copy")
    return n


def parse_var(text):
    """`KEY=VALUE` -> (KEY, VALUE). Кавычки вокруг значения снимаются; перевод
    строки -- отказ: переменная живёт одной строкой в vars.env."""
    key, sep, value = (text or "").partition("=")
    key = key.strip()
    if not sep or not _KEY.match(key):
        raise ValueError(f"{text!r}: expected VAR=VALUE")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key}: a value is one line")
    return key, value


# ─── хранилище ───────────────────────────────────────────────────────────
def project_dir(root, project):
    return os.path.join(root, project)


def _mkdir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _write(path, data):
    _mkdir(os.path.dirname(path))
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def put_file(root, project, name, data):
    name = valid_name(name)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name}: {len(data)} bytes, a secret file is at most {MAX_BYTES}")
    _mkdir(project_dir(root, project))
    _write(os.path.join(project_dir(root, project), FILES, name), data)
    return name


def remove_file(root, project, name):
    """-> был ли такой файл."""
    path = os.path.join(project_dir(root, project), FILES, valid_name(name))
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def list_files(root, project):
    base = os.path.join(project_dir(root, project), FILES)
    out = []
    for d, _, names in os.walk(base):
        for n in names:
            p = os.path.join(d, n)
            out.append({"name": os.path.relpath(p, base), "size": os.path.getsize(p)})
    return sorted(out, key=lambda f: f["name"])


def _vars(root, project):
    try:
        with open(os.path.join(project_dir(root, project), VARS)) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return {}
    out = {}
    for line in lines:
        k, sep, v = line.partition("=")
        if sep:
            out[k] = v
    return out


def _write_vars(root, project, values):
    _mkdir(project_dir(root, project))
    text = "".join(f"{k}={values[k]}\n" for k in sorted(values))
    _write(os.path.join(project_dir(root, project), VARS), text.encode())


def set_var(root, project, key, value):
    values = _vars(root, project)
    values[key] = value
    _write_vars(root, project, values)


def remove_var(root, project, key):
    """-> была ли такая переменная."""
    values = _vars(root, project)
    if key not in values:
        return False
    del values[key]
    _write_vars(root, project, values)
    return True


def list_vars(root, project):
    """Имена, без значений: список -- не способ прочитать секрет."""
    return sorted(_vars(root, project))
