"""Файловые примитивы: приватная запись и KEY=VALUE (#153).

Раньше -- пять копий записи кредов и три разборщика KEY=VALUE, и копии
разошлись: одни писали атомарно, другие усекали файл на месте, одни чинили
права лежащего файла, другие оставляли чужие. Здесь одна реализация.

Только STDLIB, без импортов из пакета: этим пользуются и config (которого
импортирует всё остальное), и агент на узле.
"""
import os
import secrets


def _write(path, data, private):
    """Временный файл рядом, fsync, rename. Читатель видит либо прежний файл
    целиком, либо новый целиком: nats-server перечитывает users.conf в любой
    момент, а обрыв посреди записи кредов оставлял усечённый файл."""
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


def write_private(path, data):
    """Файл с секретом: ровно 0600, атомарно. Каталог -- забота вызывающего:
    у каждого своё правило, каким ему быть."""
    _write(path, data, private=True)


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


def write_kv(values):
    """dict -> текст KEY=VALUE, по строке на ключ, ключи по порядку."""
    return "".join(f"{k}={values[k]}\n" for k in sorted(values))
