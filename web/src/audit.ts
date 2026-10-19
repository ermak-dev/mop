// Frozen #626/83b75ffa: конечный whitelist, не старый bus full_entry.
export interface AuditItem {
  readonly id: string;
  readonly operation_id: string;
  readonly revision: string;
  readonly created_at: string;
  readonly action: string;
  readonly state: string;
  readonly code: string;
  readonly reason: string;
  readonly target: { readonly kind: 'puppet' | 'node' | 'project'; readonly id: string; readonly incarnation: string | null };
  readonly node_id: string;
  readonly project_ids: readonly string[];
}
export interface AuditPage {
  readonly schema_version: 1;
  readonly epoch: string;
  readonly results: readonly AuditItem[];
  readonly next_cursor: string | null;
  readonly coverage: readonly ['operation_audit'];
}
export interface AuditQuery { readonly projectId?: string; readonly limit?: number }
export interface AuditView {
  readonly journal: readonly AuditItem[];
  readonly error: string | null;
  readonly loading: boolean;
  readonly nextCursor: string | null;
  readonly sourceKey: string;
}
export const AUDIT_POLL_S = 10;
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const FIELDS = ['id', 'operation_id', 'revision', 'created_at', 'action', 'state', 'code', 'reason', 'target', 'node_id', 'project_ids'];
function invalid(): never { throw new Error('Invalid operation audit response'); }
function object(value: unknown, fields: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).length !== fields.length || fields.some(key => !Object.hasOwn(row, key))) return invalid();
  return row;
}
function string(value: unknown): string {
  if (typeof value !== 'string' || value.length > 4096) return invalid();
  return value;
}
function uuid(value: unknown): string { const result = string(value); return UUID.test(result) ? result : invalid(); }
function decimal(value: unknown): string {
  const result = string(value); return /^[1-9][0-9]{0,18}$/.test(result) ? result : invalid();
}
function item(value: unknown): AuditItem {
  const row = object(value, FIELDS), target = object(row.target, ['kind', 'id', 'incarnation']);
  if (!['puppet', 'node', 'project'].includes(String(target.kind))) return invalid();
  const created_at = string(row.created_at);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(created_at) ||
      !Number.isFinite(Date.parse(created_at)) || new Date(created_at).toISOString().slice(0, 19) !== created_at.slice(0, 19)) return invalid();
  if (!Array.isArray(row.project_ids) || !row.project_ids.length || row.project_ids.length > 100) return invalid();
  const projects = row.project_ids.map(uuid);
  if (projects.some((id, i) => i > 0 && id <= projects[i - 1])) return invalid();
  const incarnation = target.incarnation === null ? null : uuid(target.incarnation);
  if (target.kind !== 'puppet' && incarnation !== null) return invalid();
  return Object.freeze({ id: decimal(row.id), operation_id: uuid(row.operation_id), revision: decimal(row.revision), created_at,
    action: string(row.action), state: string(row.state), code: string(row.code), reason: string(row.reason),
    target: Object.freeze({ kind: target.kind as AuditItem['target']['kind'], id: uuid(target.id), incarnation }),
    node_id: uuid(row.node_id), project_ids: Object.freeze(projects) });
}
export function parseAuditPage(value: unknown, limit: number): AuditPage {
  const row = object(value, ['schema_version', 'epoch', 'results', 'next_cursor', 'coverage']);
  if (row.schema_version !== 1 || !Array.isArray(row.coverage) || row.coverage.length !== 1 || row.coverage[0] !== 'operation_audit' ||
      !Array.isArray(row.results) || row.results.length > limit) return invalid();
  const results = row.results.map(item);
  if (new Set(results.map(row => row.id)).size !== results.length) return invalid();
  const cursor = row.next_cursor === null ? null : string(row.next_cursor);
  if (cursor === '') return invalid();
  return Object.freeze({ schema_version: 1, epoch: uuid(row.epoch), results: Object.freeze(results), next_cursor: cursor,
    coverage: Object.freeze(['operation_audit'] as const) });
}
export function auditQuery(query: AuditQuery = {}) {
  const limit = query.limit ?? 100, projectId = query.projectId;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 || (projectId !== undefined && !UUID.test(projectId)))
    throw new Error('Operation audit requires a canonical project id and a limit within 1..100');
  return { limit, projectId, key: JSON.stringify([projectId ?? null, limit]) };
}
// Даже диалог/clipboard не расширяет whitelist произвольными полями caller'а.
export function auditWire(entry: AuditItem): AuditItem {
  return { id: entry.id, operation_id: entry.operation_id, revision: entry.revision, created_at: entry.created_at,
    action: entry.action, state: entry.state, code: entry.code, reason: entry.reason,
    target: { kind: entry.target.kind, id: entry.target.id, incarnation: entry.target.incarnation },
    node_id: entry.node_id, project_ids: [...entry.project_ids] };
}
