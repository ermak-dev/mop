// Hook не владеет DTO/transport/cache: только lifecycle общего владельца #627.
import { useEffect } from 'react';
import type { AuditQuery, AuditView } from './audit';
import { AUDIT_POLL_S, auditQuery } from './audit';
import { poolNetwork, sessionKey, usePoolState } from './poolNetwork';
import type { Session } from './useSession';
export const POLL_S = AUDIT_POLL_S;
export interface Journal extends AuditView { more: () => Promise<void>; refresh: () => Promise<void> }
export function useJournal(session: Session, active: boolean, query: AuditQuery = {}): Journal {
  usePoolState();
  const key = sessionKey(session), queryKey = JSON.stringify([query.projectId, query.limit]);
  useEffect(() => {
    if (!active || !session.login) return;
    try { auditQuery(query); } catch { return; }
    const stop = poolNetwork.start(session), unwatch = poolNetwork.watchAudit(query);
    return () => { unwatch(); stop(); };
  }, [key, active, queryKey]);
  return { ...poolNetwork.auditView(session, active, query),
    more: () => poolNetwork.moreAudit(session, query), refresh: () => poolNetwork.refreshAudit(session, query) };
}
