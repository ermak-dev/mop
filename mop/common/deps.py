"""Python-библиотеки mop, одним списком.

Их ставят три места, и все берут список отсюда через MOP_PIP_DEPS в
--extra-vars (playvars.playbook_vars): узел (deploy/roles/bus), тело
(deploy/pve-build.yml) и сама машина контроллера или оператора
(deploy/self.yml, запускают `mop setup` и `mop server setup`). Пока список был записан в каждом
плейбуке, новая библиотека доезжала до одной машины и не доезжала до
другой, молча.

Только данные и никаких импортов: `mop setup` (и `mop server setup`) читает этот файл на машине,
где ещё ничего не стоит.
"""

# Только контроллер: клиент, узел и stdlib installer этот список не импортируют.
SERVER = (
    "Django==5.2.18",
    "djangorestframework==3.16.1",
    "channels==4.3.2",
    "uvicorn[standard]==0.54.0",
    "psycopg[binary,pool]==3.3.6",
)

PIP = (
    "nats-py",        # шина
    "aiohttp",        # её WebSocket-транспорт: wss через TLS-прокси (#97)
    "python-nomad",   # API Nomad
    "requests",       # ручки Nomad, которых python-nomad не знает
    "mcp",            # сервер инструментов мастера и папета
    "pyyaml",         # манифест .mop при сборке образа
    "ldap3",          # провайдер личности ldap (#208); импорт ленивый
    "nkeys",          # подпись JWT auth callout (#206, mop/server/nkjwt.py)
    "pynacl",         # ящики xkey и проверка подписи: nkeys 0.2.1 их не умеет
    "bcrypt",         # распознавание хэша ключа в write панели (#523, web.py)
)
