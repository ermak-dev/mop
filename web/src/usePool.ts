// Один lifecycle REST/WS на страницу; useSyncExternalStore не копирует entities.
import { useEffect } from 'react';
import type { Snapshot } from './types';
import { GUEST, type Session } from './useSession';
import { poolNetwork, sessionKey, usePoolState } from './poolNetwork';
export { POLL_MS } from './poolNetwork';
export interface Pool { snapshot: Snapshot | null; stale: boolean; error: string | null }
export function usePool(session: Session = GUEST): Pool {
  const state = usePoolState();
  const key = sessionKey(session);
  useEffect(() => poolNetwork.start(session), [key]);
  return { snapshot: poolNetwork.view(session), stale: state.connection.stale, error: poolNetwork.problem(session) };
}
