// Чистое ядро #627; адаптеры REST/WS нормализуют только разрешённые сервером
// метаданные. Здесь нет транспорта, хранения в браузере, черновиков или PTY.
// Публичный цикл: createPoolState → beginRead → reducePool(rest/event).
// Адаптер валидирует schema_version/auth и передаёт полную проекцию Entity,
// не частичный patch; kind различает проекции с разными правилами версий.
// Для refetch читаются entities с invalidated=true, для списка — collection.ids,
// для восстановления команд — recoveryRequests. head — floor, value — факт.
// Subscribe-before-snapshot и заполнение разрывов принадлежат серверу: synced
// передаётся лишь после его подтверждённого handshake, с текущим generation.
// Оpaque cursor снимка хранится только для handshake; порядок событий по нему
// клиент не угадывает. Смена прав/target/filter проходит через context, не init.
export type Json = null | boolean | number | string | readonly Json[] | { readonly [key: string]: Json };
export interface Context {
  readonly server: string;
  readonly user: string;
  readonly scope: string;
  readonly authorization: string;
  readonly target: string;
}
export interface Identity {
  readonly kind: string;
  readonly id: string;
  readonly incarnation: string | null;
  readonly revision: string;
  readonly deleted?: boolean;
}
export interface Entity extends Identity {
  readonly data: { readonly [key: string]: Json };
  readonly operation?: {
    readonly requestId: string;
    readonly status: string;
    readonly target: { readonly id: string; readonly incarnation: string };
  };
}
export type ReadQuery = { readonly type: 'detail' } | {
  readonly type: 'snapshot'; readonly kind: string;
  readonly snapshotId?: string; readonly cursor?: string;
};
export interface ApplyToken {
  readonly generation: number;
  readonly id: number;
  readonly query: ReadQuery;
  readonly snapshotGeneration?: number;
}
export interface Limits {
  readonly maxEvents: number;
  readonly maxInvalidations: number;
  readonly maxReads: number;
}
export type ResetReason = 'cursor_unknown' | 'cursor_expired' | 'gap' | 'overflow' |
  'epoch_mismatch' | 'version_conflict' | 'invalid_revision' | 'snapshot_mismatch' | 'context_changed';
export interface Slot {
  readonly head: Identity;
  readonly value?: Entity;
  readonly invalidated: boolean;
  readonly retired: readonly (string | null)[];
  readonly touched: number;
}
export interface Collection {
  readonly generation: number;
  readonly started: number;
  readonly snapshotId?: string;
  readonly cursor?: string;
  readonly next?: string | null;
  readonly complete: boolean;
  readonly ids: readonly string[];
  readonly accumulated: readonly string[];
}
export interface PoolState {
  readonly context: Context;
  readonly epoch: string;
  readonly generation: number;
  readonly clock: number;
  readonly entities: Readonly<Record<string, Slot>>;
  readonly collections: Readonly<Record<string, Collection>>;
  readonly requests: Readonly<Record<string, ApplyToken>>;
  readonly pending: Readonly<Record<string, { readonly operationId?: string }>>;
  readonly eventIds: readonly string[];
  readonly limits: Limits;
  readonly resync: ResetReason | null;
  readonly connection: { readonly status: 'disconnected' | 'connecting' | 'connected'; readonly stale: boolean; readonly error?: string };
}
export type Action =
  | { readonly type: 'rest'; readonly token: ApplyToken; readonly epoch: string; readonly rows: readonly Entity[];
      readonly page?: { readonly snapshotId: string; readonly cursor: string; readonly next: string | null } }
  | { readonly type: 'event'; readonly generation: number; readonly epoch: string; readonly eventId: string; readonly entity: Identity }
  | { readonly type: 'context'; readonly context: Context; readonly epoch: string }
  | { readonly type: 'reset'; readonly generation: number; readonly reason: ResetReason }
  | { readonly type: 'settled'; readonly token: ApplyToken }
  | { readonly type: 'track'; readonly requestId: string }
  | { readonly type: 'connection'; readonly generation: number; readonly status: PoolState['connection']['status']; readonly error?: string }
  | { readonly type: 'synced'; readonly generation: number };

export function entityKey(entity: Pick<Identity, 'kind' | 'id'>): string {
  return JSON.stringify([entity.kind, entity.id]);
}

// Замораживаем и вложенные значения; входные DTO копируются до публикации.
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
function own<T>(values: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.hasOwn(values, key) ? values[key] : undefined;
}
function without<T>(values: Readonly<Record<string, T>>, key: string): Record<string, T> {
  const copy = { ...values };
  delete copy[key];
  return copy;
}
export function createPoolState(context: Context, epoch: string, limits: Partial<Limits> = {}): PoolState {
  const bounds = { maxEvents: 512, maxInvalidations: 512, maxReads: 64, ...limits };
  if (Object.values(bounds).some(n => !Number.isSafeInteger(n) || n < 1)) throw new Error('Invalid store limits');
  return freeze({ context: { ...context }, epoch, generation: 1, clock: 0,
    entities: {}, collections: {}, requests: {}, pending: {}, eventIds: [], limits: bounds,
    resync: null, connection: { status: 'disconnected', stale: true } });
}
function sameAuth(a: Context, b: Context): boolean {
  return a.server === b.server && a.user === b.user && a.scope === b.scope && a.authorization === b.authorization;
}
function reset(state: PoolState, reason: ResetReason, context = state.context, epoch = state.epoch): PoolState {
  // Только request_id переживает пересинхронизацию в том же auth scope.
  // Это инструкция для GET recovery, никогда не очередь повторных POST.
  const pending = sameAuth(context, state.context)
    ? Object.fromEntries(Object.keys(state.pending).map(id => [id, {}])) : {};
  return freeze({ ...createPoolState(context, epoch, state.limits),
    generation: state.generation + 1, clock: state.clock + 1, pending, resync: reason });
}

// Вызывается до отправки GET или POST, ответ которого нормализуется как REST.
// Снимок — список одного kind в текущем scope. Фильтр/target входит в Context.
// Первая страница открывает новое поколение; продолжение требует точного cursor.
export function beginRead(state: PoolState, query: ReadQuery): { state: PoolState; token: ApplyToken } {
  const id = state.clock + 1;
  let collections = state.collections;
  let snapshotGeneration: number | undefined;
  if (query.type === 'snapshot') {
    const previous = own(collections, query.kind);
    if (query.snapshotId === undefined && query.cursor === undefined) {
      snapshotGeneration = id;
      collections = { ...collections, [query.kind]: {
        generation: id, started: state.clock, complete: false,
        ids: previous?.ids ?? [], accumulated: [],
      } };
    } else snapshotGeneration = previous?.generation;
  }
  const token = freeze({ generation: state.generation, id, query: structuredClone(query), snapshotGeneration });
  if (Object.keys(state.requests).length >= state.limits.maxReads) {
    return { state: reset(state, 'overflow'), token };
  }
  return { state: freeze({ ...state, clock: id, collections,
    requests: { ...state.requests, [id]: token } }), token };
}

function compare(a: string, b: string): number {
  // Канонические десятичные строки: не Number, parseInt и не cursor.
  return a.length === b.length ? (a === b ? 0 : a > b ? 1 : -1) : a.length > b.length ? 1 : -1;
}
const terminal = new Set(['succeeded', 'refused', 'expired', 'cancelled', 'failed', 'partial', 'unknown']);

// REST и invalidation проходят одну и ту же границу версий/инкарнаций.
function reconcile(state: PoolState, identity: Identity, row?: Entity, issuedAt = state.clock): PoolState {
  if (typeof identity.revision !== 'string' || !/^(0|[1-9][0-9]*)$/.test(identity.revision)) return reset(state, 'invalid_revision');
  const key = entityKey(identity);
  const previous = own(state.entities, key);
  const head: Identity = { kind: identity.kind, id: identity.id, incarnation: identity.incarnation,
    revision: identity.revision, ...(identity.deleted ? { deleted: true } : {}) };
  let order = 1;
  if (previous) {
    if (previous.retired.includes(head.incarnation)) return state;
    order = compare(head.revision, previous.head.revision);
    if (order < 0) return state;
    if (order === 0 && head.incarnation !== previous.head.incarnation) return reset(state, 'version_conflict');
    // ID удалённого ресурса нельзя выдать снова. Отсутствие в странице — не tombstone.
    if (previous.head.deleted && !head.deleted) return state;
    if (order === 0 && !head.deleted && !row) return state;
    if (order === 0 && head.deleted && previous.head.deleted) return state;
  }
  const replaced = previous && previous.head.incarnation !== head.incarnation;
  const retired = replaced ? [...previous.retired, previous.head.incarnation] : previous?.retired ?? [];
  const value = head.deleted ? undefined : row
    ? order === 0 && previous?.value?.revision === head.revision ? previous.value : structuredClone(row)
    : replaced ? undefined : previous?.value;
  // Наличие защищает порядок начала запроса, не время доставки старого ответа.
  const slot: Slot = { head, value, retired, touched: Math.max(previous?.touched ?? 0, issuedAt),
    invalidated: !head.deleted && (!value || value.revision !== head.revision) };
  let pending = state.pending;
  const op = value?.operation;
  if (op) {
    pending = !slot.invalidated && terminal.has(op.status)
      ? without(pending, op.requestId)
      : { ...pending, [op.requestId]: { operationId: head.id } };
  }
  const entities = { ...state.entities, [key]: slot };
  const count = Object.values(entities).reduce((n, item) =>
    n + Number(item.invalidated || !!item.head.deleted) + item.retired.length, 0);
  if (count > state.limits.maxInvalidations) return reset(state, 'overflow');
  const collection = own(state.collections, head.kind);
  const collections = collection ? { ...state.collections, [head.kind]: {
    ...collection, ids: head.deleted ? collection.ids.filter(id => id !== key)
      : !collection.complete || issuedAt > collection.started ? [...new Set([...collection.ids, key])] : collection.ids,
  } } : state.collections;
  return { ...state, entities, collections, pending };
}

function applyRest(state: PoolState, action: Extract<Action, { type: 'rest' }>): PoolState {
  // Используем сохранённый токен, а не поля, возвращённые вызывающим кодом.
  const token = own(state.requests, String(action.token.id));
  if (action.token.generation !== state.generation || !token) return state;
  let next: PoolState = { ...state, clock: state.clock + 1, requests: without(state.requests, String(token.id)) };
  const query = token.query;
  const collection = query.type === 'snapshot' ? own(state.collections, query.kind) : undefined;
  if (query.type === 'snapshot') {
    if (!collection || token.snapshotGeneration !== collection.generation) return freeze(next);
    if (query.snapshotId !== undefined || query.cursor !== undefined) {
      if (collection.complete || query.snapshotId !== collection.snapshotId || query.cursor !== collection.next) return freeze(next);
    } else if (collection.snapshotId !== undefined) return freeze(next);
  }
  if (action.epoch !== state.epoch) return reset(next, 'epoch_mismatch', state.context, action.epoch);
  if (query.type === 'snapshot' && collection) {
    if (!action.page || (collection.snapshotId !== undefined && action.page.snapshotId !== collection.snapshotId) ||
        (collection.cursor !== undefined && action.page.cursor !== collection.cursor) ||
        action.rows.some(row => row.kind !== query.kind)) return reset(next, 'snapshot_mismatch');
  }
  for (const row of action.rows) {
    next = reconcile(next, row, row, token.id);
    if (next.generation !== state.generation) return next;
  }
  if (query.type === 'snapshot' && collection && action.page) {
    const accumulated = [...new Set([...collection.accumulated, ...action.rows.map(entityKey)])];
    const recent = Object.entries(next.entities)
      .filter(([, slot]) => slot.head.kind === query.kind && slot.touched > collection.started)
      .map(([key]) => key);
    const complete = action.page.next === null;
    const ids = [...new Set([...(complete ? [] : collection.ids), ...accumulated, ...recent])]
      .filter(key => !next.entities[key]?.head.deleted);
    next = { ...next, collections: { ...next.collections, [query.kind]: {
      ...collection, snapshotId: action.page.snapshotId, cursor: action.page.cursor,
      next: action.page.next, complete, ids, accumulated,
    } } };
  }
  return freeze(next);
}

export function reducePool(state: PoolState, action: Action): PoolState {
  switch (action.type) {
    case 'rest': return applyRest(state, action);
    case 'event': {
      if (action.generation !== state.generation) return state;
      if (action.epoch !== state.epoch) return reset(state, 'epoch_mismatch', state.context, action.epoch);
      if (state.eventIds.includes(action.eventId)) return state;
      if (state.eventIds.length >= state.limits.maxEvents) return reset(state, 'overflow');
      const next = reconcile({ ...state, clock: state.clock + 1 }, action.entity);
      if (next.generation !== state.generation) return next;
      return freeze({ ...next, eventIds: [...state.eventIds, action.eventId] });
    }
    case 'context':
      if (sameAuth(state.context, action.context) && state.context.target === action.context.target && state.epoch === action.epoch) return state;
      return reset(state, 'context_changed', action.context, action.epoch);
    case 'reset': return action.generation === state.generation ? reset(state, action.reason) : state;
    case 'settled': return action.token.generation !== state.generation || !own(state.requests, String(action.token.id))
      ? state : freeze({ ...state, requests: without(state.requests, String(action.token.id)) });
    case 'track': return freeze({ ...state, pending: { ...state.pending, [action.requestId]: own(state.pending, action.requestId) ?? {} } });
    case 'connection': return action.generation !== state.generation ? state : freeze({ ...state, connection: { status: action.status, stale: true,
      ...(action.error === undefined ? {} : { error: action.error }) } });
    case 'synced': return action.generation !== state.generation ? state : freeze({ ...state, resync: null,
      connection: { ...state.connection, stale: false } });
  }
}

// Единственное действие восстановления команды — GET по этим request_id.
export function recoveryRequests(state: PoolState): readonly string[] { return freeze(Object.keys(state.pending)); }
