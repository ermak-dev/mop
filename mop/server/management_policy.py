"""Чистые правила management gateway; общий владелец обоих HTTP adapters."""
import json
import re
import yaml

# Management-блок, инжектируемый полному PUT (#417, вариант A): ключ
# перечитывается из закрытого файла на каждый write, остальные поля --
# безопасные дефолты управления (в config.yaml прокси их задаёт deploy).
MANAGEMENT_DEFAULTS = {"allow-remote": False, "disable-auto-update-panel": True}
SENTINEL = 'mop-proxy-managed'
PANEL_MAX = 4 * 1024 * 1024
REDIRECT_MAX = 3


def _management_paths_hit(obj, key=None, checked=None):
    """Затрагивает ли JSON-запись ЯВНОЕ поле management.secret-key: новый
    ключ -- замена, плейсхолдер или хэш текущего ключа из прочитанного
    конфига (#523) -- как отсутствие поля (браузер вернул то, что мы
    ему показали). Тело секции бывает массивом (#523): элементы
    сканируются тем же проходом -- форма не ослабляет проверку."""
    if checked is None:
        checked = {}
    def echoed(value):
        if value in checked:
            return checked[value]
        if checked:
            return False
        checked[value] = _echoed_current_key(value, key)
        return checked[value]
    if isinstance(obj, dict):
        for name, value in obj.items():
            if name == "secret-key" and not (isinstance(value, str) and
                    (value == PLACEHOLDER or echoed(value))):
                return True
            if name == "management" and not isinstance(value, dict):
                return True
            if isinstance(value, (dict, list)) and _management_paths_hit(value, key, checked):
                return True
        return False
    if isinstance(obj, list):
        return any(_management_paths_hit(item, key, checked) for item in obj)
    return False


def _bounded_tree(value, depth=0, budget=None):
    if budget is None:
        budget = [100000]
    budget[0] -= 1
    if depth > 64 or budget[0] < 0:
        raise ValueError('management document is too complex')
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        _bounded_tree(child, depth + 1, budget)


def _safe_yaml(body):
    # Alias/anchor и огромный token stream не нужны стоковому config editor.
    for count, token in enumerate(yaml.scan(body)):
        if count > 100000 or isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            raise ValueError('management YAML aliases or complexity refused')
    doc = yaml.safe_load(body)
    _bounded_tree(doc)
    return doc


def _yaml_doc(body):
    """YAML-тело -> dict | None (битое/не словарь -- None)."""
    try:
        doc = _safe_yaml(body)
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _has_explicit_secret(doc, key=None):
    """В документе есть ЯВНЫЙ secret-key (не наш плейсхолдер из чтения и
    не хэш текущего ключа, echoing назад панелью -- #523)?"""
    mgmt = doc.get("management")
    if not isinstance(mgmt, dict) or "secret-key" not in mgmt:
        return False
    value = mgmt["secret-key"]
    if isinstance(value, str) and (value == PLACEHOLDER
                                   or _echoed_current_key(value, key)):
        return False
    return True


def _yaml_has_management(body):
    try:
        doc = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    return isinstance(doc, dict) and isinstance(doc.get("management"), dict)


# Что видит браузер вместо хэша в прочитанном конфиге (#417): плейсхолдер
# не секрет и знаком guard'у -- его возврат в write читается как отсутствие
# поля, не как явная замена ключа.
PLACEHOLDER = "managed-by-mop"


def _echoed_current_key(value, key):
    """Значение secret-key -- bcrypt-хэш ТЕКУЩЕГО ключа сервера (#523)?

    Сток-панель показывает прочитанный конфиг и при сохранении возвращает
    прочитанные поля как есть: upstream отдаёт ключ хэшем, и панель несёт
    его обратно. Такой хэш -- то же, что плейсхолдер: браузер вернул то,
    что ему показали, нового ключа в записи нет. Сверка -- bcrypt по
    ключу из закрытой копии (он у guard уже есть); без bcrypt или без
    ключа хэш не распознаётся, и запись остаётся 403 -- молчаливого
    послабления guard'а нет. Новый ключ (значение мимо сверки) -- 403,
    как было (#417: ключ ведёт mop)."""
    if not isinstance(value, str) or not value or value == PLACEHOLDER or not key:
        return False
    # Стоимость контролирует browser: bounded compatibility с прежним echo.
    if not re.fullmatch(r'\$2[aby]\$(0[4-9]|1[0-2])\$[./A-Za-z0-9]{53}', value):
        return False
    try:
        import bcrypt
    except ImportError:
        return False
    try:
        return bcrypt.checkpw(key.encode(), value.encode())
    except (ValueError, TypeError):
        return False
# Что из заголовков ответа upstream уходит браузеру (#417): cookie и
# аутентификационные вызовы прокси не проходят -- сессия у нас своя.
RESPONSE_KEEP = ("Content-Type", "X-Cpa-Version", "X-Cpa-Build-Date",
                 "X-Server-Version", "X-Cpa-Support-Plugin")


def response_headers(headers):
    """Заголовки upstream -> заголовки для браузера: только whitelist."""
    names = {name.lower(): name for name in RESPONSE_KEEP}
    return {names[name.lower()]: value for name, value in (headers or {}).items()
            if name.lower() in names}


def private_material(value, key=None):
    data = value.encode('utf-8') if isinstance(value, str) else value
    return bool((key and key.encode() in data) or
                re.search(rb'\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}', data))


def _redacted(value, depth=0, budget=None, key=None):
    if budget is None:
        budget = [100000]
    budget[0] -= 1
    if depth > 64 or budget[0] < 0:
        raise ValueError('management document is too complex')
    if isinstance(value, dict):
        if 'management' in value and not isinstance(value['management'], dict):
            raise ValueError('invalid management section')
        if any(isinstance(name, str) and private_material(name, key) for name in value):
            raise ValueError('private material in management response')
        return {name: PLACEHOLDER if name == 'secret-key' else _redacted(item, depth + 1, budget, key)
                for name, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, depth + 1, budget, key) for item in value]
    if isinstance(value, str) and private_material(value, key):
        raise ValueError('private material in management response')
    return value


def redact_management_read(path, body, key=None):
    """Config читается fail closed; вложенный secret-key тоже закрывается."""
    config = path.rstrip('/').endswith('/config') or path.endswith('/config.yaml') or '/config/' in path
    if not config and not body.lstrip().startswith((b'{', b'[')):
        return None if private_material(body, key) else body
    try:
        is_yaml = path.endswith('/config.yaml')
        parsed = _safe_yaml(body) if is_yaml else json.loads(body)
        if not isinstance(parsed, (dict, list)):
            return None
        doc = _redacted(parsed, key=key)
        result = (yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True).encode()
                  if is_yaml else json.dumps(doc, ensure_ascii=False).encode())
        return result if len(result) <= PANEL_MAX else None
    except (ValueError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return None


def guard_management_write(method, path, body, key_source):
    """Решение write-guard'а. -> {action: pass|refuse|inject, status?,
    body?}.

    method/path -- запрос браузера; body -- байты; key_source -- callable
    -> plaintext-ключ из закрытого файла сервера | None (файл недоступен);
    зовётся на каждый write, ротация подхватывается без кэша. Отказ: 403
    «поле ведётся mop'ом», 400 «битое тело», 503 «ключ недоступен, а PUT
    удалил бы секрет». Инжект: management-блок получает актуальный
    secret-key; прочие management-настройки клиента сохраняются, полей
    без клиента добавляются безопасные дефолты. Сырой ключ живёт только
    в теле исходящего loopback-запроса -- не в ответах и не в логах."""
    if method not in ("POST", "PATCH", "PUT", "DELETE"):
        return {"action": "pass"}
    if not path.startswith("/v8/management/"):
        return {"action": "pass"}

    is_yaml = path.endswith("/config.yaml")
    if not (path.rstrip("/").endswith("/config") or is_yaml
            or "/config/" in path):
        return {"action": "pass"}     # не конфиг: credentials, oauth и пр.

    # Полный документ нельзя удалять/создавать без preservation-контракта;
    # панель сохраняет его PUT, отдельные настройки -- PATCH.
    if method in ("POST", "DELETE"):
        return {"action": "refuse", "status": 403}

    # path-addressed запись в само поле
    if "/config/management" in path and method == "PATCH":
        return {"action": "refuse", "status": 403}


    if is_yaml:
        doc = _yaml_doc(body)
        if doc is None:
            return {"action": "refuse", "status": 400}
        # Ключ нужен уже для распознавания хэша (#523): панель возвращает
        # прочитанное значение, и хэш текущего ключа -- «нет поля», не 403.
        key = key_source()
        has_secret = _has_explicit_secret(doc, key)
        has_mgmt = isinstance(doc.get("management"), dict)
        if has_secret:
            return {"action": "refuse", "status": 403}
        if key is None:
            return {"action": "refuse", "status": 503}
        if not has_mgmt:
            doc["management"] = {"secret-key": key, **MANAGEMENT_DEFAULTS}
        else:
            # блок клиента без явного ключа (или с нашим плейсхолдером,
            # или с хэшем текущего ключа) -- ключ сервера, прочие
            # настройки клиента сохраняются
            client = {k: v for k, v in doc["management"].items()
                      if k != "secret-key"}
            doc["management"] = {**MANAGEMENT_DEFAULTS, **client,
                                 "secret-key": key}
        return {"action": "inject",
                "body": yaml.safe_dump(doc, default_flow_style=False,
                                       allow_unicode=True).encode()}

    # JSON-ветка: PATCH -- слияние, PUT -- полная замена. Тело-массив --
    # легальная форма записи СЕКЦИИ (#523: PUT /config/api-keys/... несёт
    # список провайдеров); корневой /config остаётся dict-only.
    is_root = path.rstrip("/").endswith("/config")
    try:
        doc = json.loads(body or b"{}")
        _bounded_tree(doc)
        if isinstance(doc, list) and is_root:
            raise ValueError
        if not isinstance(doc, (dict, list)):
            raise ValueError
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {"action": "refuse", "status": 400}
    key = key_source()
    if _management_paths_hit(doc, key):
        return {"action": "refuse", "status": 403}
    # Массив секции не несёт management-блока: инжект не нужен, запись
    # идёт как есть (upstream сам знает форму секции).
    if isinstance(doc, list):
        return {"action": "pass"}
    # плейсхолдер или хэш текущего ключа из прочитанного конфига (#523) --
    # как отсутствие поля
    mgmt = doc.get("management")
    if isinstance(mgmt, dict) and isinstance(mgmt.get("secret-key"), str) \
            and (mgmt["secret-key"] == PLACEHOLDER
                 or _echoed_current_key(mgmt["secret-key"], key)):
        mgmt.pop("secret-key")
        if method == 'PATCH':
            return {'action': 'inject', 'body': json.dumps(doc, ensure_ascii=False).encode()}
    if method == "PUT":
        key = key_source()
        if key is None:
            return {"action": "refuse", "status": 503}
        client = doc.get("management") if isinstance(doc.get("management"), dict) else {}
        # настройки клиента сохраняются, ключ -- только серверный
        doc["management"] = {**MANAGEMENT_DEFAULTS, **client,
                             "secret-key": key}
        return {"action": "inject",
                "body": json.dumps(doc, ensure_ascii=False).encode()}
    return {"action": "pass"}
