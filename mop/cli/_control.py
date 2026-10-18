"""Transient boundary нового клиента; никакого .env, git config или login store.

Таблица фиксирует реальное покрытие, а не обещает отсутствующие backend API.
Старые командлеты не импортируются на новом пути даже ради разбора аргументов.
Серверные полномочия проверяет сервер; отсутствие action catalogue не даёт MCP
прав выдумать набор разрешённых человеку/гранту действий.
"""
import argparse
import contextlib
import io
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from uuid import UUID

from mop.client import control as api

_CONTEXT = ContextVar('mop_https_context', default=None)

# Конечный inventory #624/642; unsupported не означает accepted/no-op.
SURFACES = dict.fromkeys((
    'add', 'update', 'restart', 'recycle', 'delete', 'wipe', 'stop', 'send',
    'dispatch', 'list', 'detail', 'model', 'models', 'agents', 'landing', 'attach',
    'master', 'code', 'tail', 'stat', 'project add', 'project delete', 'project limit',
    'node', 'node forget', 'secret file', 'secret var', 'build', 'driver build',
    'session history', 'session terminal', 'server doctor', 'server gc', 'server sweep',
    'server disk', 'server proxykey', 'server user', 'server user add', 'server user delete',
    'server user passwd', 'server user import', 'login', 'mcp'), 'unsupported')
SURFACES.update(dict.fromkeys(('health', 'profile', 'diagnose', 'project list', 'node drain',
                              'node up', 'operation', 'addresses', 'stat', 'build', 'list', 'detail', 'restart',
                              'wipe', 'stop', 'delete', 'add', 'send', 'landing'), 'bound'))

# Владельцы незаконченных bindings: #625 lifecycle, #626 projections,
# #628 physical stream, #648 builds, #649 inbox, #656 usage.
GAPS = {
    'agents': 'live address aggregation and private endpoint inbox binding',
    'update': 'update admission',
    'recycle': 'recycle admission',
    'dispatch': 'dispatch admission and scoped target selection',
    'model': 'session model action', 'models': 'inference model query',
    'attach': 'private terminal physical resolver/capability discovery',
    'session terminal': 'private terminal physical resolver/capability discovery',
    'session history': 'private history query',
    'master': 'role creation/readiness and private WS terminal',
    'code': 'explicit inference-only launcher context',
    'project add': 'project registration/build API',
    'driver build': 'approved local-source policy preparation for durable image builds',
    'node': 'authorized node projection query',
    'node forget': 'safe forget action',
    'login': 'Django token issuance client (no connection.json store)',
}


def _prefix(argv, *, legacy=False):
    selected, index, old_selector = {}, 0, False
    while index < len(argv):
        word = argv[index]
        name = word.split('=', 1)[0]
        if name in ('--api-origin', '--api-ca'):
            key = 'origin' if name == '--api-origin' else 'ca_file'
            if key in selected:
                raise api.ClientError('duplicate HTTPS context option')
        elif legacy and name == '--server':
            key, old_selector = None, True
        elif name.startswith('--api-'):
            raise api.ClientError('unsupported HTTPS context option')
        else:
            break
        if '=' in word:
            value = word.split('=', 1)[1]
        else:
            index += 1
            value = argv[index] if index < len(argv) else ''
        if not value or value.startswith('-'):
            raise api.ClientError('context option requires a value')
        if key is not None:
            selected[key] = value
        index += 1
    return selected, list(argv[index:]), old_selector


def options(argv):
    """Только global prefix; command data не становится trust/token selector."""
    selected, rest, _ = _prefix(argv)
    return selected, rest


def dispatch_options(argv):
    """Legacy selector нужен только для запрета до context/.env, не для HTTP."""
    return _prefix(argv, legacy=True)


def route(modname, argv):
    """Resolved command -> finite HTTPS surface; dev/setup/service остаются локальны."""
    if modname == 'mop.cli.project':
        return 'project list'
    if modname == 'mop.cli.service.mcp':
        return 'mcp'
    if modname == 'mop.cli.core._launch':
        return 'master'
    if modname.startswith('mop.cli.core.'):
        name = modname.rsplit('.', 1)[-1]
        return name if name in SURFACES else None
    for group in ('project', 'node', 'secret', 'session'):
        prefix = 'mop.cli.' + group
        if modname == prefix:
            return group if group in SURFACES else None
        if modname.startswith(prefix + '.'):
            name = group + ' ' + modname.rsplit('.', 1)[-1]
            return name if name in SURFACES else None
    # #713: recursive rotation leaves принадлежат одному ещё неподключённому
    # surface. Leaf не должен уходить в legacy context через общий server case.
    if modname == 'mop.cli.server.proxykey' or modname.startswith('mop.cli.server.proxykey.'):
        return 'server proxykey'
    if modname == 'mop.cli.server.user':
        return 'server user'
    if modname.startswith('mop.cli.server.user.'):
        name = 'server user ' + modname.rsplit('.', 1)[-1]
        return name if name in SURFACES else None
    if modname.startswith('mop.cli.server.'):
        name = 'server ' + modname.rsplit('.', 1)[-1]
        return name if name in SURFACES else None
    if modname == 'mop.cli.driver.build':
        return 'driver build'
    if modname == 'mop.cli.pool.login':
        return 'login'
    return None


@contextmanager
def use(context):
    """Внутрипроцессный context для MCP/tests; не хранится и не сериализуется."""
    if type(context) is not api.ServerContext:
        raise api.ClientError('explicit HTTPS server context required')
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def missing(origin=None):
    """Каких частей transient-контекста нет; при выбранном контексте -- ничего."""
    if _CONTEXT.get() is not None:
        return []
    return [name for name, value in (('MOP_API_ORIGIN', origin or os.environ.get('MOP_API_ORIGIN')),
                                     ('MOP_API_TOKEN', os.environ.get('MOP_API_TOKEN'))) if not value]


def context(origin=None, ca_file=None):
    selected = _CONTEXT.get()
    if selected is not None:
        if origin is not None or ca_file is not None:
            raise api.ClientError('server context is already selected')
        return selected
    # Причина отказа -- имя недостающей переменной, не общее «invalid input».
    absent = missing(origin)
    if absent:
        raise api.ClientError('no HTTPS server context: set ' + ' and '.join(absent) +
                              ' (the origin may also come from --api-origin)')
    # Это явный transient input, не config.get: .env и профили не читаются.
    return api.ServerContext(origin or os.environ.get('MOP_API_ORIGIN', ''),
        os.environ.get('MOP_API_TOKEN', ''), ca_file or os.environ.get('MOP_API_CA') or None)


def plain(value):
    if hasattr(value, '__dataclass_fields__'):
        return plain(asdict(value))
    if isinstance(value, dict) or hasattr(value, 'items'):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# Лишь делегируемые действия; человеческий override/terminal/node Admin не
# появляется у гранта от имени роли или от прав human owner.
_GRANT_ACTIONS = frozenset('execution.' + action for action in (
    'create_puppet', 'update_puppet', 'restart_puppet', 'recycle_puppet', 'delete_puppet',
    'wipe_puppet', 'stop_allocation', 'send_message', 'dispatch_task', 'view_delivery',
    'deliver_report', 'send_session_command', 'change_session_model', 'view_usage',
    'view_landing', 'acquire_landing', 'release_landing')) | frozenset((
    'catalog.view_project', 'catalog.view_pool', 'operations.view_operation',
    'access.view_role', 'controller.view_models'))
_TOOL_LOCK = threading.RLock()


def allowed(declaration, identity):
    actions = declaration.get('actions')
    if (type(identity) is not api.Identity or identity.action_permissions is None or
            not isinstance(actions, list) or
            any(not isinstance(action, str) for action in actions)):
        return False
    if not actions and declaration.get('annotations') != 'readonly':
        return False
    required = frozenset(actions)
    if declaration.get('human_only') is True and identity.kind != 'human':
        return False
    delegated = identity.grant_id is not None
    if delegated and identity.kind == 'service' and not required <= _GRANT_ACTIONS:
        return False
    return (identity.kind in ('human', 'service') if delegated
            else identity.kind in ('human', 'agent')) and required <= identity.action_permissions


def tool_call(words, declaration, values):
    """MCP вызывает тот же dispatcher/commandlet, а не второй API adapter.

    Блокировка нужна из-за process-global stdout; команда не исполняется в
    фоне после возврата unknown. Key не меняется и не повторяется скрытым retry.
    """
    from mop import cli
    if SURFACES.get(' '.join(words)) != 'bound':
        raise api.ClientError('unsupported MCP commandlet')
    argv = cli.tool_argv(declaration.get('args', []), values)
    out, err = io.StringIO(), io.StringIO()
    with _TOOL_LOCK, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([*words, *argv])
        except SystemExit as error:
            code = error.code
    text = out.getvalue() + err.getvalue()
    return text.rstrip() or ('done' if code == 0 else 'command refused')


def emit(value, *, credential):
    text = json.dumps(plain(value), ensure_ascii=True, separators=(',', ':'))
    if credential:
        text = text.replace(credential, '[redacted]')
    print(text)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse обычно отражает весь ошибочный argv, включая private input.
        raise api.ClientError('invalid command arguments')


def parser(words):
    result = Parser(prog='mop ' + words)
    return result


def _operation_options(p, *, binding_required=True):
    p.add_argument('--request-id')
    p.add_argument('--epoch', required=binding_required)
    p.add_argument('--expected-revision', required=binding_required)
    p.add_argument('--not-after')
    p.add_argument('--wait', type=float)


def _resource_id(value):
    """Send принимает только canonical UUID; имя/сокет не становятся local target."""
    valid = False
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        pass
    if not valid:
        raise api.ClientError('send requires canonical resource ids, not local recipients or puppet names')
    return value


def _render_operation(operation, *, credential):
    # Только canonical receipt; никакого stdout исходного message/credential.
    emit({'schema_version': operation.schema_version, 'epoch': operation.epoch,
          'operation': plain(operation.data)}, credential=credential)
    return 0 if operation.state == 'succeeded' else 3 if operation.state in (
        'accepted', 'running', 'reconciling', 'unknown', 'partial') else 1


def _failure(error, credential):
    if isinstance(error, api.APIError):
        code, reason = error.error.code, error.error.reason
        text = reason if reason.startswith(code + ':') else code + ': ' + reason
    else:
        text = str(error)
    return text.replace(credential, '[redacted]') if credential else text


def _operation_note(operation):
    print('operation observation unavailable; operation_id=' + operation.id +
          '; request_id=' + operation.request_id + '; epoch=' + operation.epoch, file=sys.stderr)


def _landing_recovery(client, intent, credential):
    """Никакого повторного POST: даже совпадение — observation, не receipt."""
    print('landing command outcome unknown; read-only recovery is not a command receipt; '
          'do not retake automatically; project_id=' + intent.project_id + '; epoch=' + intent.epoch +
          '; expected_revision=' + intent.expected_revision + '; owner_id=' + str(intent.owner_id), file=sys.stderr)
    try:
        recovery = client.recover_landing(intent)
    except api.ClientError as error:
        print('landing observation failed: ' + _failure(error, credential), file=sys.stderr)
        return 3
    emit({**recovery.snapshot.envelope(), 'recovery': {'matched': recovery.matched,
          'command_outcome': 'unknown', 'historical_receipt': False}}, credential=credential)
    return 3


def invoke(words, argv, *, origin=None, ca_file=None):
    """Один CLI handler для dispatcher и MCP; stdout — только ответ команды."""
    operation = selected = lookup_failure = None
    try:
        if SURFACES.get(words) != 'bound' and words != 'mcp':
            gap = GAPS.get(words, 'finite authorized ' + words + ' backend API')
            print('unsupported: ' + gap + ' is not bound in this release', file=sys.stderr)
            return 2
        p = parser(words)
        if words == 'mcp':
            p.add_argument('--check', action='store_true')
            args = p.parse_args(argv)
            if not args.check:
                print('unsupported: server action-permission catalogue and private inbox lifecycle are not bound',
                      file=sys.stderr)
                return 2
        elif words in ('health', 'profile', 'diagnose'):
            args = p.parse_args(argv)
        elif words == 'stat':
            p.add_argument('--days', type=int)
            p.add_argument('--start')
            p.add_argument('--end')
            p.add_argument('--group-by', choices=('day', 'key', 'model', 'user', 'project', 'puppet'), default='day')
            p.add_argument('--users', action='store_true')
            p.add_argument('--puppets', action='store_true')
            args = p.parse_args(argv)
            if args.users or args.puppets or args.group_by in ('user', 'project', 'puppet'):
                print('unsupported: user, project and puppet usage attribution is unavailable for shared proxy keys',
                      file=sys.stderr)
                return 2
            if bool(args.start) != bool(args.end) or args.start and args.days is not None:
                raise api.ClientError('provide start/end or days, not both')
            if not args.start:
                days = 14 if args.days is None else args.days
                if not 1 <= days <= 366:
                    raise api.ClientError('usage days must be within 1..366')
                end = datetime.now(timezone.utc).date() + timedelta(days=1)
                args.start, args.end = (end - timedelta(days=days)).isoformat(), end.isoformat()
        elif words == 'landing':
            p.add_argument('action', choices=('show', 'take', 'give', 'recover-take', 'recover-give'))
            p.add_argument('project_id')
            p.add_argument('--epoch')
            p.add_argument('--expected-revision')
            p.add_argument('--owner-id', type=int)
            args = p.parse_args(argv)
            if args.action == 'show':
                if args.expected_revision is not None or args.owner_id is not None:
                    raise api.ClientError('landing show accepts only project id and optional epoch')
                api._key(args.project_id)
                if args.epoch is not None:
                    api._key(args.epoch)
            else:
                args.command = 'take' if args.action.endswith('take') else 'release'
                api.landing_binding(args.command, args.project_id, args.epoch, args.expected_revision)
                if args.action.startswith('recover-'):
                    prepare = api.LandingIntent(args.command, args.project_id, args.epoch,
                        args.expected_revision, args.owner_id)
                elif args.owner_id is not None:
                    raise api.ClientError('landing mutation derives the actual human actor, not an owner-id argument')
        elif words == 'addresses':
            p.add_argument('project_id')
            p.add_argument('--limit', type=int, default=100)
            args = p.parse_args(argv)
        elif words == 'list':
            p.add_argument('--project-id')
            p.add_argument('--cursor')
            p.add_argument('--page-size', type=int, default=100)
            p.add_argument('--all-pages', action='store_true')
            args = p.parse_args(argv)
            if args.all_pages and args.cursor is not None:
                raise api.ClientError('all-pages starts a fresh snapshot; do not combine with cursor')
        elif words == 'detail':
            p.add_argument('puppet_id')
            args = p.parse_args(argv)
        elif words == 'project list':
            p.add_argument('--cursor')
            p.add_argument('--page-size', type=int, default=100)
            args = p.parse_args(argv)
        elif words == 'send':
            # Эти опции требуют иной authority/holder lifecycle, не скрытого
            # перевода на bus или UDS. Отказ даже до чтения private stdin.
            unsupported = {'--list', '--local', '--mode', '--force', '--notify-when-idle', '--reply-to'}
            options = argv[:argv.index('--')] if '--' in argv else argv
            if (argv and argv[0] == 'list') or any(word.split('=', 1)[0] in unsupported for word in options):
                print('unsupported: local recipients/listing, legacy routing/permission mode, takeover and '
                      'idle notifications (trusted holder endpoint is not bound)', file=sys.stderr)
                return 2
            p.add_argument('puppet_id', type=_resource_id)
            p.add_argument('message')
            p.add_argument('--priority', choices=('now', 'next', 'later'), default='next')
            p.add_argument('--wait-seconds', type=int, default=0)
            p.add_argument('--incarnation', type=_resource_id)
            _operation_options(p, binding_required=False)
            args = p.parse_args(argv)
            args.request_id = api.new_request_id(args.request_id)
            if args.not_after is None:
                args.not_after = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
            api.admission_deadline(args.not_after)
            values = (args.epoch, args.incarnation, args.expected_revision)
            args.explicit_binding = all(value is not None for value in values)
            if any(value is not None for value in values) and not args.explicit_binding:
                raise api.ClientError('provide epoch, incarnation and expected-revision together')
            if args.explicit_binding:
                _resource_id(args.epoch)
                target = api.Target(args.puppet_id, args.incarnation, args.expected_revision)
            if not 0 <= args.wait_seconds <= 60:
                raise api.ClientError('wait-seconds must be within 0..60; it is not an idle/delivery receipt')
        elif words in ('restart', 'wipe', 'stop', 'delete'):
            p.add_argument('puppet_id')
            p.add_argument('--incarnation')
            if words == 'restart':
                p.add_argument('--fresh', action='store_true')
            p.add_argument('--force', action='store_true')
            _operation_options(p, binding_required=False)
            args = p.parse_args(argv)
            if getattr(args, 'fresh', False) or args.force:
                print('unsupported: fresh restart and privileged assignment takeover are not bound'
                      if words == 'restart' else 'unsupported: privileged assignment takeover is not bound',
                      file=sys.stderr)
                return 2
            args.request_id = api.new_request_id(args.request_id)
            if args.not_after is None:
                args.not_after = (datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
            values = (args.epoch, args.incarnation, args.expected_revision)
            args.explicit_binding = all(value is not None for value in values)
            if any(value is not None for value in values) and not args.explicit_binding:
                raise api.ClientError('provide epoch, incarnation and expected-revision together')
            if args.explicit_binding:
                prepare = api.PreparedCommand(words, api.Target(args.puppet_id, args.incarnation,
                    args.expected_revision), args.epoch, args.request_id, args.not_after)
        elif words == 'add':
            p.add_argument('project_id')
            p.add_argument('--node-id', required=True)
            p.add_argument('--definition-id', required=True)
            p.add_argument('--epoch', required=True)
            p.add_argument('--request-id')
            p.add_argument('--not-after')
            p.add_argument('--wait', type=float)
            args = p.parse_args(argv)
            # Create не имеет target/CAS. Весь intent фиксируется до context/TLS.
            args.request_id = api.new_request_id(args.request_id)
            if args.not_after is None:
                args.not_after = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
            prepare = api.CreateCommand(args.project_id, args.node_id, args.definition_id,
                args.epoch, args.request_id, args.not_after)
        elif words == 'build':
            p.add_argument('project_id')
            p.add_argument('--policy-id', required=True)
            p.add_argument('--target-node', action='append', required=True)
            p.add_argument('--mode', choices=('missing', 'update', 'rebuild'), default='missing')
            p.add_argument('--force', action='store_true')
            p.add_argument('--force-safety', action='store_true')
            p.add_argument('--branch')
            _operation_options(p)
            args = p.parse_args(argv)
            if args.force or args.force_safety or args.branch is not None:
                print('unsupported: privileged build recovery and branch policy approval are not bound', file=sys.stderr)
                return 2
            args.request_id = api.new_request_id(args.request_id)
            if args.not_after is None:
                args.not_after = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            prepare = api.BuildCommand(args.project_id, args.policy_id, args.epoch, args.request_id,
                args.not_after, args.expected_revision, tuple(args.target_node), args.mode)
        elif words in ('node drain', 'node up'):
            p.add_argument('node_id')
            _operation_options(p)
            args = p.parse_args(argv)
            # Ключ фиксируется до создания TLS context и любого I/O.
            args.request_id = api.new_request_id(args.request_id)
            if args.not_after is None:
                args.not_after = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
            prepare = api.NodeCommand('drain' if words == 'node drain' else 'enable', args.node_id,
                args.epoch, args.request_id, args.not_after, args.expected_revision)
        elif words == 'operation':
            p.add_argument('operation_id', nargs='?')
            p.add_argument('--request-id')
            p.add_argument('--epoch')
            p.add_argument('--wait', type=float)
            args = p.parse_args(argv)
            if bool(args.operation_id) == bool(args.request_id) or args.request_id and not args.epoch:
                raise api.ClientError('provide operation id or request-id with epoch')
        if hasattr(args, 'wait') and args.wait is not None:
            if not 0 < args.wait <= api.Limits().wait_timeout:
                raise api.ClientError('wait must be within the client wait limit')
        if words == 'send':
            if args.message == '-':
                try:
                    # TextIO может декодировать несколько bytes на символ. После
                    # bounded read проверяем ещё UTF-8 byte limit; newline не режем.
                    args.message = sys.stdin.read(api.MAX_MESSAGE_BYTES + 1)
                except (OSError, UnicodeError):
                    raise api.ClientError('message stdin is unavailable or invalid UTF-8') from None
            args.message = api.message_text(args.message)
            if args.explicit_binding:
                prepare = api.PreparedCommand('send', target, args.epoch, args.request_id,
                    args.not_after, message=args.message, priority=args.priority, wait_seconds=args.wait_seconds)
        if words == 'diagnose' and missing(origin):
            # Как empty у прежней сводки (#536): not-ready -- информация, не отказ.
            emit({'schema_version': 1, 'ready': False, 'missing': missing(origin),
                  'network_observed': False}, credential=None)
            return 0
        selected = context(origin, ca_file)
        if words == 'diagnose':
            emit({'schema_version': 1, 'ready': True, 'origin': selected.origin, 'credential_source': 'transient context',
                  'trust': 'explicit CA (unverified)' if selected.ca_file else 'system CA (unverified)',
                  'network_observed': False}, credential=selected.token)
            return 0
        client = api.Client(selected)
        if words in ('restart', 'wipe', 'stop', 'delete', 'send') and not args.explicit_binding:
            view = client.read_puppet(args.puppet_id)
            params = dict(message=args.message, priority=args.priority,
                          wait_seconds=args.wait_seconds) if words == 'send' else {}
            prepare = {'restart': client.prepare_restart, 'wipe': client.prepare_wipe,
                       'stop': client.prepare_stop, 'delete': client.prepare_delete,
                       'send': client.prepare_send}[words](view.target, epoch=view.epoch,
                           not_after=args.not_after, request_id=args.request_id, **params)
        if words == 'health':
            emit({'schema_version': 1, **asdict(client.health())}, credential=selected.token)
        elif words in ('profile', 'mcp'):
            emit({'schema_version': 1, **plain(client.identity())}, credential=selected.token)
        elif words == 'stat':
            emit(client.proxy_usage(start=args.start, end=args.end, group_by=args.group_by),
                 credential=selected.token)
        elif words == 'landing':
            if args.action == 'show':
                emit(client.read_landing(args.project_id, epoch=args.epoch).envelope(), credential=selected.token)
            elif args.action.startswith('recover-'):
                return _landing_recovery(client, prepare, selected.token)
            else:
                try:
                    value = client.change_landing(args.command, args.project_id, epoch=args.epoch,
                        expected_revision=args.expected_revision)
                except api.UnknownLanding as error:
                    return _landing_recovery(client, error.intent, selected.token)
                emit(value.envelope(), credential=selected.token)
        elif words == 'addresses':
            emit({'schema_version': 1, 'addresses': plain(client.addresses(args.project_id, limit=args.limit).items)},
                 credential=selected.token)
        elif words == 'list':
            if args.all_pages:
                for page in client.puppet_pages(project_id=args.project_id, page_size=args.page_size):
                    emit(page.data, credential=selected.token)
            else:
                page = client.puppets_page(project_id=args.project_id, cursor=args.cursor, page_size=args.page_size)
                emit(page.data, credential=selected.token)
        elif words == 'detail':
            view = client.read_puppet(args.puppet_id)
            emit({'schema_version': view.schema_version, 'epoch': view.epoch, 'puppet': view.data},
                 credential=selected.token)
        elif words == 'project list':
            page = client.projects_page(cursor=args.cursor, page_size=args.page_size)
            emit({'schema_version': 1, 'results': plain(page.items), 'next_cursor': page.next_cursor},
                 credential=selected.token)
        elif words == 'operation':
            operation = (client.lookup_operation(args.request_id, epoch=args.epoch) if args.request_id
                         else client.operation(args.operation_id))
            if args.epoch is not None and operation.epoch != args.epoch:
                raise api.ProtocolError('operation epoch mismatch')
            if args.wait is not None and not operation.terminal:
                operation = client.wait_operation(operation.id, timeout=args.wait, initial=operation)
            return _render_operation(operation, credential=selected.token)
        else:
            try:
                operation = client.submit(prepare).operation
            except api.UnknownSubmission:
                # Не делаем POST retry, даже под старым ключом; lookup только GET.
                try:
                    operation = client.recover(prepare)
                except api.ClientError as failure:
                    # 404 lookup тоже не доказывает отсутствие admission COMMIT,
                    # но безопасная причина чтения не должна исчезнуть в unknown.
                    lookup_failure = failure
                    raise api.UnknownSubmission(prepare) from None
            if args.wait is not None and not operation.terminal:
                operation = client.wait_operation(operation.id, timeout=args.wait, initial=operation)
            return _render_operation(operation, credential=selected.token)
        return 0
    except api.UnknownSubmission as error:
        print('submission unknown; request_id=' + error.request_id + '; epoch=' + error.command.epoch +
              '; recover with operation lookup; do not repeat the effect', file=sys.stderr)
        if lookup_failure is not None:
            print('lookup failed: ' + _failure(lookup_failure, selected.token), file=sys.stderr)
        return 3
    except api.WaitTimeout as error:
        if error.operation is not None:
            _render_operation(error.operation, credential=selected.token)
        print('wait expired; operation is not cancelled', file=sys.stderr)
        return 3
    except api.APIError as error:
        print(_failure(error, selected.token), file=sys.stderr)
        if operation is not None:
            _operation_note(operation)
        return 1
    except api.ClientError as error:
        print(_failure(error, selected.token if selected else None), file=sys.stderr)
        if operation is not None:
            _operation_note(operation)
            return 3
        return 1
