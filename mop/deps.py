"""Зависимости самой машины: контроллера и машины оператора.

Ansible ставит зависимости узлам (deploy/roles/node) и телам (pve-build.yml),
а машине, с которой запускают mop, — никому: единственный контроллер
обживался руками, и потребности не возникало, пока контроллер не собрался
переезжать на сервер. Здесь один список и план «что ставить» по роли и по
тому, что уже есть; ставит `mop setup`, проверяет tests/deps.py.

Ролей две. Контроллер гоняет плейбуки: ansible с коллекцией ansible.posix
(ради synchronize), rsync на своей стороне того же synchronize, git и curl.
Оператор плейбуков не гоняет, но `mop mcp`, `mop join` и `mop master` ходят
на те же python-библиотеки, что и контроллер, — список pip один.
"""

# (пакет pip, имя импорта): проверяется импорт, потому что pip list дорог и
# врёт про пакеты, поставленные apt'ом.
PIP = (
    ("nats-py", "nats"),           # шина
    ("python-nomad", "nomad"),     # API Nomad
    ("requests", "requests"),      # ручки Nomad, которых python-nomad не знает
    ("mcp", "mcp"),                # сервер инструментов мастера и папета
    ("pyyaml", "yaml"),            # манифест .mop при сборке образа
)

APT = {
    "controller": ("ansible", "rsync", "git", "curl"),
    "operator": ("git",),
}
# Коллекции ansible: у контроллера, вместе с ansible. Проверяются командой
# ansible-galaxy по имени коллекции, как и бинарники, — без ansible их
# проверить нечем, и они идут в план вместе с ним.
GALAXY = {"controller": ("ansible.posix",), "operator": ()}


def plan(role, have_command, have_module):
    """Что ставить на эту машину. -> {apt: [...], galaxy: [...], pip: [...]}

    have_command(имя) и have_module(имя) — факты об этой машине, снаружи,
    чтобы план был чистым. Роль узла здесь не существует: узлы — дело
    плейбуков, и второй список для них завёлся бы молча."""
    if role not in APT:
        raise ValueError(f"no such role: {role}; controller or operator")
    return {
        "apt": [p for p in APT[role] if not have_command(p)],
        "galaxy": [c for c in GALAXY[role] if not have_command(c)],
        "pip": [p for p, mod in PIP if not have_module(mod)],
    }
