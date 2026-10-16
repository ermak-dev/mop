"""mop list [--project-id <id>] [--cursor VALUE] [--page-size N] [--all-pages]

Read the controller's authorized pool projection, not a client-built roster.
One bounded page is returned with its snapshot id, opaque next cursor and
coverage. --all-pages emits canonical pages of the same snapshot as separate
JSON lines. It never polls nodes or invents facts missing from the server.

Unknown observations, unavailable usage/endpoints/pending-operation coverage and
an incomplete realtime handshake remain explicit. Silent does not mean free.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": ["catalog.view_pool"], "args": [
    {"name": "project_id", "type": "string", "flag": "--project-id", "help": "authorized project resource id"},
    {"name": "cursor", "type": "string", "flag": "--cursor", "help": "opaque page cursor from the server"},
    {"name": "page_size", "type": "integer", "flag": "--page-size", "help": "page size within 1..500"},
    {"name": "all_pages", "type": "boolean", "flag": "--all-pages", "help": "emit bounded canonical pages without building another roster"}]}

# Чистый formatter прежнего PuppetRow нужен старым локальным render-проверкам;
# сетевого reader и решения о доступности здесь больше нет.
WIDTHS = (15, 7, 8, 46, 10, 7)


def place(row):
    """Прочерк — неизвестно, не измеренный ноль."""
    kb = row.disk_kb
    if kb is None:
        return '-'
    return f'{kb / 2**20:.0f} GB' if kb >= 2**20 else f'{kb / 2**10:.0f} MB'


def line(row):
    value = row.render()
    cells = (value['name'], value['node'], value['alloc_status'], value['state'], value['owner'], place(row))
    return '  '.join(cell.ljust(width) for cell, width in zip(cells, WIDTHS)) + '  ' + value['origin']


def main(argv):
    return _control.invoke('list', argv)
