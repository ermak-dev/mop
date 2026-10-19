"""Management-only stdio #642: декларации командлетов, не второй pool adapter.

Private inbox #649 выключен без явного trusted owner port. Контекст только
transient HTTPS; ни discovery, ни исполнение не импортируют прежнюю шину или
role bundle. MCP не запускает receiver и не владеет его lifetime.
"""
import functools
import threading

from mop import cli
from mop.client import control as api
from . import _control

PRIVATE_INBOX = {'available': False, 'reason': 'not_bound', 'issue': 649}
INSTRUCTIONS = ('Authenticated management tools only. Actions are advisory and refreshed before each call; '
    'the server authorizes actual resources. Private report submission and receipt queries are finite tools; '
    'queued submissions are not native acceptance. Receiver/holder inbox unavailable by default (#649): '
    'no agents roster, endpoint registration or native receiver is provided by this stdio server.')


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
    def __init__(self, context, scope, *, limits=None, validate=None, guard=None, on_failure=None, inbox=None):
        # Только trusted composition передаёт уже существующего owner. Ни MCP
        # creation, ни tool call не запускают receiver и не владеют его teardown.
        if inbox is not None:
            from mop.client.private_inbox import InboxOwner
            if (type(inbox) is not InboxOwner or inbox.context != context or
                    scope.project_id != inbox.project_id or scope.client_id != inbox.client_id):
                raise api.ClientError('trusted inbox differs from management context or scope')
        self.inbox = inbox
        self.context, self.scope = context, scope
        self.limits = limits or api.Limits()
        self.validate, self.guard, self.on_failure = validate, guard, on_failure
        self.client = api.Client(context, limits=self.limits, before_request=guard)
        self.commands = commandlets()
        self.lock = threading.RLock()
        initial = self.client.capabilities(scope)
        if inbox is not None and initial.identity.kind != 'human':
            raise api.ClientError('Agent management cannot borrow a human private inbox')
        if self.validate is not None:
            self.validate(initial)
        self.binding = (initial.epoch, initial.identity.user_id, initial.identity.kind,
            initial.identity.human_owner_id, initial.identity.node_id, initial.identity.grant_id)

    def private_status(self):
        """Конечный snapshot текущего port, не initialization capability cache."""
        if (self.inbox is not None and self.inbox.epoch == self.binding[0]
                and self.inbox.holder() is not None):
            return {'available': True, 'reason': 'bound', 'issue': 649}
        return dict(PRIVATE_INBOX)

    def refresh(self):
        try:
            if self.guard is not None:
                self.guard()
            current = self.client.capabilities(self.scope)
            identity = current.identity
            if (current.epoch, identity.user_id, identity.kind, identity.human_owner_id,
                    identity.node_id, identity.grant_id) != self.binding:
                raise api.ClientError('MCP actor or installation changed; restart with current context')
            if self.validate is not None:
                self.validate(current)
            return current
        except Exception as error:
            if self.on_failure is not None:
                self.on_failure(error)
            raise

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
            if self.guard is not None:
                self.guard()
            with _control.use(self.context, limits=self.limits, before_request=self.guard, inbox=self.inbox):
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


def serve_body(bootstrap, context, deadline):
    """SDK initialize не ждёт activation. Только настоящий catalogue будит tools."""
    import anyio
    import time
    from mcp import types
    from mcp.server.lowlevel import Server, NotificationOptions
    from mcp.server.stdio import stdio_server
    from mop.client.body_management import BodyManagement, SerializedOwner, UNAVAILABLE

    management = BodyManagement(bootstrap, context, deadline, Management)

    async def run():
        import os
        # SDK AsyncFile.readline ждёт blocking stdin worker даже при cancel.
        # Передаём только отменяемый reader; framing/JSON/stdio остаются у SDK.
        class Input:
            def __init__(self):
                self.fd, self.buffer, self.eof = os.dup(0), bytearray(), False
                null = os.open(os.devnull, os.O_RDONLY)
                try:
                    os.dup2(null, 0)
                finally:
                    os.close(null)

            def __aiter__(self):
                return self

            async def __anext__(self):
                while b'\n' not in self.buffer and not self.eof:
                    await anyio.wait_readable(self.fd)
                    chunk = os.read(self.fd, 65536)
                    self.eof = not chunk
                    self.buffer.extend(chunk)
                    if len(self.buffer) > 1024 * 1024:
                        raise api.ClientError('body MCP input exceeds size bound')
                if not self.buffer:
                    raise StopAsyncIteration
                end = self.buffer.find(b'\n')
                end = len(self.buffer) if end < 0 else end+1
                line = bytes(self.buffer[:end]); del self.buffer[:end]
                return line.decode('utf-8', errors='replace')

            def close(self):
                try:
                    os.dup2(self.fd, 0)
                finally:
                    os.close(self.fd)

        reader = Input()
        owner = SerializedOwner(management)
        initialized = anyio.Event()
        session = None

        async def ready(ctx, params):
            nonlocal session
            session = ctx.session
            initialized.set()

        async def list_tools(ctx, params):
            try:
                tools = await owner.request('tools')
            except api.ClientError:
                tools = []
            return types.ListToolsResult(tools=tools)

        async def call_tool(ctx, params):
            try:
                return await owner.request('call', params.name, params.arguments or {})
            except api.ClientError:
                return types.CallToolResult(isError=True,
                    content=[types.TextContent(type='text', text=UNAVAILABLE)])

        server = Server('mop', instructions=('Body standby has no identity or authority until a verified active '
            'Agent catalogue is bound. '+INSTRUCTIONS), on_list_tools=list_tools, on_call_tool=call_tool)
        server.add_notification_handler('notifications/initialized', types.NotificationParams, ready)
        options = server.create_initialization_options(NotificationOptions(tools_changed=True),
            experimental_capabilities={'private_inbox': PRIVATE_INBOX})

        async def watch():
            known, active = (), False
            while True:
                if not active and time.monotonic() >= management.deadline:
                    raise api.ClientError('body management handoff deadline exceeded')
                if initialized.is_set():
                    try:
                        tools = await owner.request('tools')
                    except api.ClientError:
                        tools = []
                    if not active and management.state == 'failed':
                        raise api.ClientError(management.failure or UNAVAILABLE)
                    names = tuple(tool.name for tool in tools)
                    if management.state == 'active':
                        active = True
                    if names != known and (active or known):
                        await session.send_tool_list_changed()
                        known = names
                await anyio.sleep(1 if active else .1)

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(watch)
                async with stdio_server(stdin=reader) as (read, write):
                    await server.run(read, write, options)
                group.cancel_scope.cancel()
        finally:
            # Ни EOF, ни repeated cancel не оставляют I/O/temp CA за SDK.
            try:
                with anyio.CancelScope(shield=True):
                    await owner.aclose()
            finally:
                reader.close()

    try:
        anyio.run(run)
    except Exception:
        raise api.ClientError(management.failure or 'body management handoff failed or deadline exceeded') from None
    return 0
