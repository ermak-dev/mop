// Только внешний wire frozen #626 (83b75ffa), без старого bus full_entry.
import { EPOCH, ID, INCARNATION, OP, PROJECT } from './network-test-utils';
export const NODE = '00000000-0000-4000-8000-000000000006';
export const auditItem = (id = '9007199254740993', reason = 'target does not receive dispatch') => ({
  id, operation_id: OP, revision: '9007199254740995', created_at: '2026-10-18T10:05:00Z',
  action: 'dispatch', state: 'refused', code: 'dispatch_declined', reason,
  target: { kind: 'puppet' as const, id: ID, incarnation: INCARNATION }, node_id: NODE, project_ids: [PROJECT],
});
export const auditPage = (results = [auditItem()], next_cursor: string | null = null, epoch = EPOCH) => ({
  schema_version: 1, epoch, results, next_cursor, coverage: ['operation_audit'],
});
export const auditError = (code = 'permission_denied', message = 'Current audit permission is required') => ({
  schema_version: 1, epoch: EPOCH, error: { code, message, retryable: false }, reset: { required: true },
});
