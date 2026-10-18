"""Management-only stdio #642: декларации командлетов, не второй pool adapter.

Private inbox #649 намеренно отсутствует. Контекст только transient HTTPS;
ни discovery, ни исполнение не импортируют прежнюю шину или role bundle.
"""
import functools
import threading

from mop import cli
from mop.client import control as api
from . import _control

PRIVATE_INBOX = {'available': False, 'reason': 'not_bound', 'issue': 649}
INSTRUCTIONS = ('Authenticated management tools only. Actions are advisory and refreshed before each call; '
    'the server authorizes actual resources. private inbox unavailable (#649): no agents, report, idle '
    'notifications, endpoint registration or native delivery acceptance is provided.')


def loud(fn):
    """Причина отказа в ответе; недоверенные исключения не раскрывают private input."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from mcp import types
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            reason = str(error) if isinstance(error, api.ClientError) else 'tool execution failed'
            context = getattr(args[0], 'context', None) if args else None
            if type(context) is api.ServerContext:
                reason = reason.replace(context.token, '[redacted]')
            return types.CallToolResult(isError=True,
                content=[types.TextContent(type='text', text=fn.__name__ + ': ' + reason)])
    return wrapped


def commandlets():
    """Только bound leaves; AST-declaration — единственный контракт инструмента."""
    result = {}
    for name, words, path in cli.tool_commands(cli.scan(), cli.verbs()):
        if _control.SURFACES.get(' '.join(words)) != 'bound':
            continue
        declaration = cli.declared(path)
        if declaration is not None and 'actions' in declaration:
            if name in result:
                raise api.ClientError('duplicate MCP commandlet name')
            result[name] = (words, path, declaration)
    return result


def schema(declaration):
    properties, required = {}, []
    for arg in declaration.get('args', []):
        shape = {'type': arg['type'], 'description': arg.get('help', '')}
        if arg.get('repeatable'):
            shape = {'type': 'array', 'items': {'type': 'string'}, 'description': arg.get('help', '')}
        properties[arg['name']] = shape
        if arg.get('required'):
            required.append(arg['name'])
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def arguments(declaration, values):
    if type(values) is not dict:
        raise api.ClientError('invalid tool arguments')
    declared = {arg['name']: arg for arg in declaration.get('args', [])}
    if set(values) - set(declared):
        raise api.ClientError('unknown tool arguments')
    for name, arg in declared.items():
        value = values.get(name)
        if value is None and not arg.get('required'):
            continue
        expected = {'string': str, 'integer': int, 'boolean': bool}[arg['type']]
        valid = (type(value) is list and all(type(item) is str for item in value)
                 if arg.get('repeatable') else type(value) is expected)
        if not valid:
            raise api.ClientError('invalid tool argument type')
    return dict(values)


class Management:
    def __init__(self, context, scope):
        self.context, self.scope = context, scope
        self.client = api.Client(context)
        self.commands = commandlets()
        self.lock = threading.RLock()
        initial = self.client.capabilities(scope)
        self.binding = (initial.epoch, initial.identity.user_id, initial.identity.kind,
            initial.identity.human_owner_id, initial.identity.node_id, initial.identity.grant_id)

    def refresh(self):
        current = self.client.capabilities(self.scope)
        identity = current.identity
        if (current.epoch, identity.user_id, identity.kind, identity.human_owner_id,
                identity.node_id, identity.grant_id) != self.binding:
            raise api.ClientError('MCP actor or installation changed; restart with current context')
        return current

    def tools(self):
        from mcp import types
        with self.lock:
            identity = self.refresh().identity
            result = []
            for name, (_, path, declaration) in self.commands.items():
                if not _control.allowed(declaration, identity):
                    continue
                readonly = declaration.get('annotations') == 'readonly'
                result.append(types.Tool(name=name, description=cli.docstring(path),
                    inputSchema=schema(declaration), annotations=types.ToolAnnotations(
                        readOnlyHint=readonly, destructiveHint=not readonly)))
            return result

    @loud
    def call(self, name, values):
        from mcp import types
        with self.lock:
            current = self.refresh()
            entry = self.commands.get(name)
            if entry is None or not _control.allowed(entry[2], current.identity):
                raise api.ClientError('tool not authorized in current scope')
            words, _, declaration = entry
            values = arguments(declaration, values)
            # Явный project selector нельзя опустить до installation-wide query.
            # Другие resource ids проверяет сервер, а не guessed client scope.
            for field in ('project_id', 'node_id', 'client_id'):
                selected = getattr(self.scope, field)
                if selected is not None and field in schema(declaration)['properties']:
                    if values.get(field) not in (None, selected):
                        raise api.ClientError('tool scope differs from discovery context')
                    values[field] = selected
            if 'epoch' in values and values['epoch'] != current.epoch:
                raise api.ClientError('tool epoch differs from discovery context')
            with _control.use(self.context):
                text, status = _control.tool_call(words, declaration, values, with_status=True)
            text = text.replace(self.context.token, '[redacted]')
            # accepted/pending и unknown остаются честным operation результатом,
            # а отказ parser/API — ошибкой инструмента.
            return types.CallToolResult(isError=status not in (0, 3),
                content=[types.TextContent(type='text', text=text)])


def serve(context, scope):
    """SDK владеет stdio. Весь stdout командлетов захватывает общий tool_call."""
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    management = Management(context, scope)

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=await anyio.to_thread.run_sync(management.tools))

    async def call_tool(ctx, params):
        return await anyio.to_thread.run_sync(management.call, params.name, params.arguments or {})

    server = Server('mop', instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)
    options = server.create_initialization_options(experimental_capabilities={'private_inbox': PRIVATE_INBOX})

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, options)
    anyio.run(run)
    return 0
