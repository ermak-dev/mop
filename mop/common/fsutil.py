"""Файловые примитивы: приватная запись и KEY=VALUE (#153).

Раньше -- пять копий записи кредов и три разборщика KEY=VALUE, и копии
разошлись: одни писали атомарно, другие усекали файл на месте, одни чинили
права лежащего файла, другие оставляли чужие. Здесь одна реализация.

Только STDLIB, без импортов из пакета: этим пользуются и config (которого
импортирует всё остальное), и агент на узле.
"""
import json
import os
import secrets


def _write(path, data, private, owner=None):
    """Временный файл рядом, fsync, rename. Читатель видит либо прежний файл
    целиком, либо новый целиком: nats-server перечитывает users.conf в любой
    момент, а обрыв посреди записи кредов оставлял усечённый файл.

    owner -- (uid, gid) файла, отданный до первого байта (#234): не выходит
    -- OSError, и на месте остаётся прежний файл, а не чужой."""
    raw = data.encode() if isinstance(data, str) else data
    # Имя с солью, а не `path.tmp`: два писателя одного файла не пишут в
    # один временный, а брошенный после обрыва не мешает следующей записи.
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 0o600 if private else 0o666)
    try:
        if private:
            # 0600 до первого байта и независимо от umask: mode у open
            # umask только урезает.
            os.fchmod(fd, 0o600)
        if owner is not None:
            st = os.fstat(fd)
            if (st.st_uid, st.st_gid) != tuple(owner):
                os.fchown(fd, *owner)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def write_private(path, data, owner=None):
    """Файл с секретом: ровно 0600, атомарно. Каталог -- забота вызывающего:
    у каждого своё правило, каким ему быть. owner -- как у _write."""
    _write(path, data, private=True, owner=owner)


def write_atomic(path, data):
    """Не секрет, но и усечённым его видеть нельзя (реестр проектов, лимиты):
    атомарно, права -- по umask, как у обычного open."""
    _write(path, data, private=False)


def make_private_dir(path):
    """Каталог 0700, в том числе уже лежащий: mode у makedirs действует только
    на новый последний уровень и урезается umask'ом."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def read_kv(text, raw=False):
    """KEY=VALUE построчно -> dict; повтор ключа -- последний.

    Диалекта два, и оба нужны:
      .env (по умолчанию) -- .env, node.env, secrets.env: пустые строки и
          `#`-комментарии пропускаются, пробелы по краям ключа и значения
          срезаются, с краёв значения -- и кавычки. Формат намеренно
          примитивен: его читают и питон, и ansible, и sed.
      raw -- vars.env секретов проекта и вывод пробы агента: значение байт в
          байт, как записано. vars.env пишем мы сами (write_kv), и значение
          с пробелами или кавычками обязано вернуться тем же.
    Строка без `=` пропускается в обоих."""
    out = {}
    for line in text.splitlines():
        if not raw:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if raw:
            out[k] = v
        else:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read_json(path, default=None):
    """JSON файла либо default: файла нет, не читается или не JSON (#317).
    Форму (словарь, нужные ключи) проверяет вызывающий -- у каждого своя."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_kv(values):
    """dict -> текст KEY=VALUE, по строке на ключ, ключи по порядку."""
    return "".join(f"{k}={values[k]}\n" for k in sorted(values))
