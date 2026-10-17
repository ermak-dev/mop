// Один владелец подтверждённых DTO; компоненты хранят лишь выбор/черновик.
// WS несёт invalidation, не полный Puppet и не PTY. REST проходит тот же reducer.
import { useSyncExternalStore } from 'react';
import { hybridRequest, HybridHttpError, reasonOf } from './hybridApi';
import { beginRead, createPoolState, entityKey, reducePool, recoveryRequests } from './poolStore';
import type { ApplyToken, Entity, Identity, Json, ReadQuery } from './poolStore';
import type { Session } from './useSession';
import type { Counts, Kind, Puppet, Snapshot, VerdictKind } from './types';
import { operations } from './useOperationFeed';

export const POLL_MS = 5_000;
const TERMINAL = new Set(['succeeded', 'failed', 'refused', 'expired', 'cancelled', 'partial', 'unknown']);
type ObjectValue = Record<string, unknown>;
function object(value: unknown): ObjectValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid API object');
  return value as ObjectValue;
}
function text(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 4096) throw new Error('Invalid API string');
  return value;
}
function envelope(value: unknown): ObjectValue {
  const body = object(value);
  if (body.schema_version !== 1) throw new Error('Unsupported API schema');
  return body;
}
function identity(raw: unknown, kind?: string): Identity {
  const row = object(raw);
  const revision = text(row.revision);
  if (!/^(0|[1-9][0-9]*)$/.test(revision)) throw new Error('Invalid entity revision');
  if (row.incarnation !== null && row.incarnation !== undefined && typeof row.incarnation !== 'string') throw new Error('Invalid incarnation');
  return { kind: kind ?? text(row.kind), id: text(row.id), incarnation: row.incarnation == null ? null : text(row.incarnation),
    revision, deleted: row.deleted === true };
}
function entity(raw: unknown, kind?: string): Entity {
  const row = object(raw), head = identity(raw, kind);
  if (head.kind === 'operation') {
    const target = object(row.target);
    return { ...head, data: row as Record<string, Json>, operation: { requestId: text(row.request_id), status: text(row.state),
      target: { id: text(target.id), incarnation: typeof target.incarnation === 'string' ? target.incarnation : '' } } };
  }
  if (head.kind !== 'puppet') throw new Error('Unsupported projection kind');
  object(row.desired); object(row.observed); object(row.state);
  return { ...head, data: row as Record<string, Json> };
}
export const sessionKey = (session: Session) => JSON.stringify([session.login, session.role, session.generation ?? '']);
const context = (key: string) => ({ server: window.location.origin, user: key, scope: 'authorized-projects',
  authorization: key, credentialGeneration: key, target: '' });

class PoolNetwork {
  state = createPoolState(context(''), '');
  key = '';
  private listeners = new Set<() => void>();
  private sockets = new Map<string, WebSocket>();
  private readyProjects = new Set<string>();
  private syncedProjects = new Set<string>();
  private handshaking = false;
  private closeSockets() {
    for (const socket of this.sockets.values()) socket.close();
    this.sockets.clear(); this.readyProjects.clear(); this.syncedProjects.clear(); this.handshaking = false;
  }
  private controller = new AbortController();
  private timer: ReturnType<typeof setInterval> | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;
  private owners = 0;
  private loading = false;
  private sequence = 0;
  private details = new Set<string>();
  private reading = new Set<string>();
  private snapshotReady = false;
  private coverage: unknown = null;
  private presentation: Snapshot | null = null;
  private error: string | null = null;
  private detailErrors = new Map<string, string>();
  private now = Date.now();

  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  get = () => this.state;
  private publish() {
    this.state = { ...this.state };
    this.presentation = this.snapshotReady ? this.present() : null;
    for (const listener of this.listeners) listener();
  }
  private dispatch(action: Parameters<typeof reducePool>[1]) {
    const before = this.state.generation;
    this.state = reducePool(this.state, action);
    if (before !== this.state.generation) { this.snapshotReady = false; this.detailErrors.clear(); }
    this.publish();
  }
  private begin(query: ReadQuery): ApplyToken {
    const result = beginRead(this.state, query);
    this.state = result.state;
    return result.token;
  }
  matches(session: Session) { return this.key === sessionKey(session) && !!session.login; }
  view(session: Session) { return this.matches(session) ? this.presentation : null; }
  problem(session: Session) { return this.matches(session) ? this.error : null; }
  detailError(session: Session, id: string) { return this.matches(session) ? this.detailErrors.get(id) ?? null : null; }
  puppet(id: string): Entity | undefined {
    const slot = this.state.entities[entityKey({ kind: 'puppet', id })];
    return slot?.head.deleted ? undefined : slot?.value;
  }
  operation(requestId: string): Entity | undefined {
    return Object.values(this.state.entities).find(slot => slot.value?.operation?.requestId === requestId && !slot.invalidated)?.value;
  }
  start(session: Session) {
    const key = sessionKey(session);
    if (this.key !== key || this.owners === 0) {
      this.stop(); this.key = key;
      this.state = reducePool(this.state, { type: 'context', context: context(key), epoch: '' });
      this.snapshotReady = false; this.presentation = null; this.error = null; this.details.clear(); this.detailErrors.clear();
      this.controller = new AbortController(); this.publish();
      if (session.login) {
        void this.connect();
        this.timer = setInterval(() => { this.now = Date.now(); this.publish(); void this.reconcile(); }, POLL_MS);
      }
    }
    this.owners++;
    return () => { if (this.key === key && --this.owners === 0) { this.stop(); this.snapshotReady = false; this.publish(); } };
  }
  private stop() {
    this.sequence++; this.controller.abort(); this.closeSockets();
    if (this.timer) clearInterval(this.timer);
    if (this.retry) clearTimeout(this.retry);
    this.timer = null; this.retry = null; this.owners = 0; this.loading = false; this.reading.clear();
    operations.clear();
  }
  private async request(url: string, body?: unknown) {
    return envelope(await hybridRequest<unknown>(url, body, this.controller.signal));
  }
  private failure(error: unknown, generation: number) {
    if (generation !== this.state.generation || this.controller.signal.aborted) return;
    this.error = reasonOf(error);
    if (error instanceof HybridHttpError && (error.code === 'reset_required' || error.code === 'epoch_mismatch')) {
      this.reset('cursor_unknown'); return;
    }
    if (error instanceof HybridHttpError && (error.status === 401 || (error.status === 403 && ['not_authenticated', 'authentication_failed', 'permission_denied'].includes(error.code ?? '')))) {
      // Запрет чтения стирает приватные факты, а не оставляет их на экране до reload.
      this.dispatch({ type: 'reset', generation, reason: 'context_changed' });
      this.sequence++; this.closeSockets(); this.loading = false;
    }
    this.dispatch({ type: 'connection', generation: this.state.generation, status: 'disconnected', error: this.error });
  }
  private reset(reason: 'cursor_unknown' | 'overflow' | 'gap') {
    this.dispatch({ type: 'reset', generation: this.state.generation, reason });
    this.sequence++; this.loading = false; this.reading.clear();
    this.closeSockets();
    void this.connect();
  }
  private async connect() {
    const at = ++this.sequence, generation = this.state.generation;
    this.dispatch({ type: 'connection', generation, status: 'connecting' });
    try {
      const projects: string[] = [], seen = new Set<string>();
      let cursor: string | null = null;
      do {
        const page = await this.request('/api/v1/projects' + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''));
        if (at !== this.sequence) return;
        if (!Array.isArray(page.results)) throw new Error('Invalid projects page');
        for (const row of page.results) projects.push(text(object(row).id));
        cursor = page.next_cursor == null ? null : text(page.next_cursor);
        if (cursor && seen.has(cursor)) throw new Error('Repeated projects cursor');
        if (cursor) seen.add(cursor);
        if (seen.size > 100) throw new Error('Projects pagination overflow');
      } while (cursor);
      // Gateway ограничивает audience 16 проектами. Без ложного global cursor:
      // REST остаётся доступным, transport limitation явно виден в stale/error.
      if (!projects.length || projects.length > 16) {
        this.error = 'Realtime requires between one and sixteen authorized projects';
        await this.load(); return;
      }
      for (const project of projects) {
        const url = new URL('/ws/v1/events', window.location.href);
        url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
        url.searchParams.set('project_id', project);
        const socket = new WebSocket(url.toString()); this.sockets.set(project, socket);
        socket.onmessage = event => {
          if (at !== this.sequence) return;
          void this.frame(event.data, project, at).catch(error => { this.failure(error, this.state.generation); this.reset('gap'); });
        };
        const disconnected = () => {
          if (at !== this.sequence) return;
          this.sequence++; this.closeSockets(); this.loading = false;
          this.dispatch({ type: 'connection', generation: this.state.generation, status: 'disconnected' });
          if (!this.retry) this.retry = setTimeout(() => { this.retry = null; void this.connect(); }, 1000);
        };
        socket.onclose = disconnected; socket.onerror = disconnected;
      }
    } catch (error) { this.failure(error, generation); }
  }
  private async frame(raw: unknown, project: string, at: number) {
    if (typeof raw !== 'string' || raw.length > 131072) { this.reset('overflow'); return; }
    const body = object(JSON.parse(raw));
    if (body.type === 'reset_required') { this.reset('cursor_unknown'); return; }
    envelope(body);
    if (body.type === 'ready') {
      this.readyProjects.add(project);
      if (this.readyProjects.size !== this.sockets.size || this.handshaking) return;
      this.handshaking = true;
      await this.load();
      if (at !== this.sequence || !this.snapshotReady) return;
      for (const [audience, socket] of this.sockets) {
        // Actual ProjectionHistory принимает ровно один project. Глобальный
        // snapshot cursor подходит лишь если весь scope состоит из одного.
        const cursor = this.sockets.size === 1 ? this.state.collections.puppet?.cursor : await this.projectCursor(audience, at);
        if (at !== this.sequence) return;
        if (cursor) socket.send(JSON.stringify({ type: 'subscribe', cursor }));
      }
      await this.recover(); return;
    }
    if (body.type !== 'events' || !Array.isArray(body.results) || body.results.length > 256 || body.handshake_complete !== true || !Array.isArray(body.coverage)) throw new Error('Invalid events frame');
    const generation = this.state.generation;
    for (const rawEvent of body.results) {
      const event = object(rawEvent);
      this.dispatch({ type: 'event', generation, epoch: text(event.epoch), eventId: text(event.event_id), entity: identity(event.entity) });
      if (this.state.generation !== generation) { this.reset('gap'); return; }
    }
    if (text(body.epoch) !== this.state.epoch) { this.reset('gap'); return; }
    this.syncedProjects.add(project);
    if (this.syncedProjects.size === this.sockets.size) {
      this.dispatch({ type: 'connection', generation, status: 'connected' });
      // Это handshake только перечисленных publication owners, не всех данных пула.
      this.dispatch({ type: 'synced', generation });
    }
    await this.refreshInvalidated();
  }
  private async projectCursor(project: string, at: number): Promise<string | undefined> {
    let next: string | null = null, snapshotId: string | undefined, cursor: string | undefined;
    const seen = new Set<string>();
    do {
      const token = this.begin({ type: 'detail' });
      try {
        const query = new URLSearchParams({ project_id: project });
        if (next) query.set('cursor', next);
        const body = await this.request(`/api/v1/snapshot?${query}`);
        if (at !== this.sequence || token.generation !== this.state.generation) return;
        if (!Array.isArray(body.results) || (snapshotId && snapshotId !== body.snapshot_id) || (cursor && cursor !== body.cursor)) throw new Error('Scoped snapshot changed during pagination');
        snapshotId = text(body.snapshot_id); cursor = text(body.cursor);
        const rows = body.results.map(row => entity(row));
        if (rows.some(row => row.data.project_id !== project)) throw new Error('Scoped snapshot returned another project');
        this.dispatch({ type: 'rest', token, epoch: text(body.epoch), rows });
        if (token.generation !== this.state.generation) { this.reset('gap'); return; }
        next = body.next_cursor == null ? null : text(body.next_cursor);
        if (next && (seen.has(next) || seen.size >= 100)) throw new Error('Scoped snapshot pagination overflow');
        if (next) seen.add(next);
      } catch (error) { this.dispatch({ type: 'settled', token }); throw error; }
    } while (next);
    return cursor;
  }
  private async load() {
    if (this.loading || this.controller.signal.aborted) return;
    this.loading = true;
    const at = this.sequence;
    let token = this.begin({ type: 'snapshot', kind: 'puppet' });
    try {
      let cursor: string | null = null;
      do {
        const body = await this.request('/api/v1/snapshot' + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''));
        if (at !== this.sequence || token.generation !== this.state.generation) return;
        const epoch = text(body.epoch);
        if (!this.state.epoch) {
          // До первого snapshot нет подписки и принятых entity; установка epoch
          // не пропускает прежние callbacks через поколение нового контекста.
          this.dispatch({ type: 'context', context: this.state.context, epoch });
          token = this.begin({ type: 'snapshot', kind: 'puppet' });
        }
        if (!Array.isArray(body.results)) throw new Error('Invalid snapshot rows');
        const next = body.next_cursor == null ? null : text(body.next_cursor);
        const snapshotId = text(body.snapshot_id), vector = text(body.cursor);
        const generation = this.state.generation;
        this.dispatch({ type: 'rest', token, epoch, rows: body.results.map(row => entity(row)),
          page: { snapshotId, cursor: vector, next } });
        if (this.state.generation !== generation) { this.reset('gap'); return; }
        this.coverage = body.coverage;
        if (!next) break;
        cursor = next; token = this.begin({ type: 'snapshot', kind: 'puppet', snapshotId, cursor });
      } while (true);
      this.snapshotReady = true; this.error = null; this.publish();
      await Promise.all([...this.details].map(id => this.readDetail(id)));
    } catch (error) { this.dispatch({ type: 'settled', token }); this.failure(error, token.generation); }
    finally { if (at === this.sequence) this.loading = false; }
  }
  watchDetail(id: string) {
    this.details.add(id);
    if (this.snapshotReady) void this.readDetail(id);
    return () => { this.details.delete(id); };
  }
  private async readDetail(id: string) {
    if (this.reading.has(id) || !this.state.epoch) return;
    this.reading.add(id);
    const token = this.begin({ type: 'detail' });
    try {
      const body = await this.request(`/api/v1/puppets/${encodeURIComponent(id)}`);
      const row = entity(body.puppet);
      if (row.id !== id) throw new Error('Detail target changed');
      this.dispatch({ type: 'rest', token, epoch: text(body.epoch), rows: [row] });
      if (token.generation === this.state.generation) this.detailErrors.delete(id);
    } catch (error) {
      this.dispatch({ type: 'settled', token });
      if (token.generation === this.state.generation) { this.detailErrors.set(id, reasonOf(error)); this.failure(error, token.generation); }
    } finally { this.reading.delete(id); this.publish(); }
  }
  private async refreshInvalidated() {
    await Promise.all(Object.values(this.state.entities).filter(slot => slot.invalidated).map(slot =>
      slot.head.kind === 'puppet' ? this.readDetail(slot.head.id) : this.readOperation(slot.head.id)));
  }
  private async readOperation(id: string) {
    const token = this.begin({ type: 'detail' });
    try { this.applyOperation(await this.request(`/api/v1/operations/${encodeURIComponent(id)}`), token); }
    catch (error) { this.dispatch({ type: 'settled', token }); this.failure(error, token.generation); }
  }
  private applyOperation(body: ObjectValue, token: ApplyToken) {
    const row = entity(body.operation, 'operation');
    if (token.generation === this.state.generation && this.state.pending[row.operation!.requestId]) operations.accepted(row.operation!.target.id, row.id);
    this.dispatch({ type: 'rest', token, epoch: text(body.epoch), rows: [row] });
    const current = this.operation(row.operation!.requestId);
    if (current && TERMINAL.has(current.operation!.status)) operations.frame({ id: current.id,
      action: String(current.data.action ?? ''), target: current.operation!.target.id,
      status: current.operation!.status, error: typeof current.data.reason === 'string' ? current.data.reason : undefined });
    return current;
  }
  track(requestId: string) {
    if (!this.operation(requestId) && !this.state.pending[requestId]) this.dispatch({ type: 'track', requestId });
  }
  async recover() {
    if (!this.state.epoch) return;
    await Promise.all(recoveryRequests(this.state).map(async requestId => {
      const token = this.begin({ type: 'detail' });
      try {
        const pending = this.state.pending[requestId];
        const path = pending?.operationId ? `/api/v1/operations/${encodeURIComponent(pending.operationId)}`
          : `/api/v1/operations?request_id=${encodeURIComponent(requestId)}&epoch=${encodeURIComponent(this.state.epoch)}`;
        this.applyOperation(await this.request(path), token);
      } catch (error) {
        this.dispatch({ type: 'settled', token });
        // 404 не доказательство отсутствия COMMIT/эффекта: request остаётся для GET.
        if (!(error instanceof HybridHttpError && [404, 410].includes(error.status))) this.failure(error, token.generation);
      }
    }));
  }
  private async reconcile() {
    if (this.controller.signal.aborted) return;
    await this.load(); await this.refreshInvalidated(); await this.recover();
    if (!this.sockets.size && !this.retry) void this.connect();
  }
  async command(id: string, action: string, params: Record<string, unknown>, requestId: string) {
    const row = this.puppet(id);
    if (!row || this.state.entities[entityKey(row)].invalidated) throw new Error('Refresh the target before submitting');
    // Конечный набор команд, которые контроллер принимает (#625/#642); прочие
    // (add, update, recycle, model) честно отказываются до POST.
    const route = PUPPET_COMMANDS[action];
    if (!route) throw new Error(`Command ${action} is not implemented by the controller`);
    if (!row.incarnation) throw new Error('Target has no active incarnation');
    const commandParams = route.params(params);
    const desired = object(row.data.desired);
    const expected = text(desired.revision);
    if (!/^(0|[1-9][0-9]*)$/.test(expected)) throw new Error('Invalid assignment revision');
    const body: Record<string, unknown> = { schema_version: 1, epoch: this.state.epoch, request_id: requestId,
      incarnation: row.incarnation, expected_revision: expected,
      not_after: new Date(Date.now() + 5 * 60_000).toISOString(), params: commandParams };
    if (route.targetInBody) body.target_id = id;
    this.dispatch({ type: 'track', requestId });
    const token = this.begin({ type: 'detail' });
    try {
      const result = this.applyOperation(await this.request(route.path(id), body), token);
      if (result) operations.accepted(id, result.id);
      await this.recover(); return result?.id ?? null;
    } catch (error) {
      this.dispatch({ type: 'settled', token });
      // Никогда не повторять POST для выяснения судьбы даже после timeout/403.
      await this.recover();
      if (this.operation(requestId)) return this.operation(requestId)!.id;
      throw error;
    }
  }
  private present(): Snapshot {
    const counts: Counts = { puppets: 0, free: 0, busy: 0, sick: 0, silent: 0, down: 0 };
    const projects = new Map<string, Snapshot['projects'][number]>();
    for (const key of this.state.collections.puppet?.ids ?? []) {
      const slot = this.state.entities[key];
      if (!slot?.value || slot.head.deleted) continue;
      const puppet = presentPuppet(slot.value, this.now, slot.invalidated);
      const projectId = String(slot.value.data.project_id), projectName = String(slot.value.data.project_name);
      let project = projects.get(projectId);
      if (!project) { project = { name: projectName, puppets: [], counts: { ...counts, puppets: 0, free: 0, busy: 0, sick: 0, silent: 0, down: 0 } }; projects.set(projectId, project); }
      project.puppets.push(puppet); project.counts.puppets++; counts.puppets++;
      const bucket = puppet.bucket!; counts[bucket]++; project.counts[bucket]++;
    }
    return { at: null, projects: [...projects.values()], counts, nodes: [], usage: [], per_puppet: [], per_user: [], masters: [],
      errors: [...(this.error ? [this.error] : []), `Read coverage: ${JSON.stringify(this.coverage)}; node capacity, private endpoints and proxy usage are not part of this snapshot`] };
  }
}
type CommandRoute = { path: (id: string) => string; params: (input: Record<string, unknown>) => Record<string, unknown>; targetInBody?: boolean };
const empty = (action: string) => (input: Record<string, unknown>) => {
  // Ни force, ни fresh: перехват и свежая сессия не подключены (#642).
  if (Object.keys(input).length) throw new Error(`Command ${action} takes no parameters`);
  return {};
};
const MESSAGE_KEYS = new Set(['message', 'priority', 'wait_seconds', 'notify_when_idle']);
const message = (input: Record<string, unknown>) => {
  if (typeof input.message !== 'string' || !input.message || Object.keys(input).some(key => !MESSAGE_KEYS.has(key)))
    throw new Error('A message with only priority, wait and idle-notification options is required');
  return { ...input };
};
const puppetPath = (verb: string) => (id: string) => `/api/v1/puppets/${encodeURIComponent(id)}/commands/${verb}`;
export const PUPPET_COMMANDS: Record<string, CommandRoute> = {
  restart: { path: puppetPath('restart'), params: (input) => {
    if (Object.keys(input).some(key => key !== 'fresh') || (input.fresh !== undefined && input.fresh !== false))
      throw new Error('Only controlled host-session restart is supported');
    return { fresh: false };
  } },
  wipe: { path: puppetPath('wipe'), params: empty('wipe') },
  delete: { path: puppetPath('delete'), params: empty('delete') },
  stop: { path: puppetPath('stop-job'), params: empty('stop') },
  send: { path: () => '/api/v1/messages', params: message, targetInBody: true },
  dispatch: { path: () => '/api/v1/dispatches', params: message, targetInBody: true },
};
/** Принимает ли контроллер команду цели (#627): то, чего нет в таблице, отказывается до POST. */
export const admits = (action: string) => Object.hasOwn(PUPPET_COMMANDS, action);
export function presentPuppet(row: Entity, now = Date.now(), invalidated = false): Puppet {
  const data = row.data as ObjectValue, desired = object(data.desired), state = object(data.state), observed = object(data.observed);
  const stateFact = object(observed.state ?? {}), clone = object(observed.clone ?? {});
  const expired = (fact: ObjectValue) => fact.freshness !== 'fresh' || typeof fact.expires_at !== 'string' || Date.parse(fact.expires_at) <= now;
  const stale = invalidated || expired(stateFact) || expired(clone);
  const kind = (stale ? 'unknown' : state.kind) as VerdictKind;
  const bucket: Kind = kind === 'free' || kind === 'idle' ? 'free' : kind === 'silent' || kind === 'unknown' ? 'silent'
    : ['hung', 'error', 'failing', 'quota', 'login'].includes(kind ?? '') ? 'sick' : 'busy';
  return { name: row.id, node: String(data.node_name), alloc_status: 'unverified', state: kind ?? 'unknown', kind, bucket,
    detail: stale ? 'Current observations unavailable; refresh required' : typeof state.detail === 'string' ? state.detail : null,
    branch: !stale && typeof state.branch === 'string' ? state.branch : null,
    owner: typeof desired.owner_id === 'string' ? desired.owner_id : '', llm: 'unavailable', origin: 'unavailable', disk_kb: null,
    soul: typeof desired.role === 'string' ? desired.role : null,
    soul_digest: typeof desired.bundle_digest === 'string' ? desired.bundle_digest : null, soul_inc: row.incarnation };
}
export const poolNetwork = new PoolNetwork();
export function usePoolState() { return useSyncExternalStore(poolNetwork.subscribe, poolNetwork.get); }
