import { useCallback, useEffect, useRef, useState } from 'react';
import type { Session } from './useSession';
import { emptyHistory, mergeHistory } from './hybridProtocol';
import type { HistoryPage, HistoryState } from './hybridProtocol';
import { HybridHttpError, hybridRequest, reasonOf } from './hybridApi';
interface View { history: HistoryState; loading: boolean; error: string | null; hasMore: boolean; denied: boolean }
const initial: View = { history: emptyHistory, loading: false, error: null, hasMore: false, denied: false };
function validPage(page: HistoryPage): boolean {
  return !!page && typeof page.epoch === 'string' && typeof page.session_id === 'string' && typeof page.cursor === 'string'
    && typeof page.reset === 'boolean' && typeof page.partial === 'boolean' && typeof page.has_more === 'boolean'
    && Array.isArray(page.records) && page.records.every(record => record !== null && typeof record === 'object' && !Array.isArray(record));
}
export function useHybridHistory(session: Session, target: string, enabled: boolean) {
  const [view, setView] = useState<View>(initial);
  const current = useRef(initial);
  const generation = useRef(0);
  const pending = useRef<Promise<void> | null>(null);
  const blocked = useRef(false);
  const abort = useRef<AbortController | null>(null);
  const identity = `${session.login}\0${session.role}\0${target}\0${enabled}`;
  const identityRef = useRef(identity); identityRef.current = identity;
  const update = useCallback((next: View) => { current.current = next; setView(next); }, []);
  const load = useCallback((): Promise<void> => {
    if (!enabled || session.role !== 'admin' || !session.login || blocked.current) return Promise.resolve();
    if (pending.current) return pending.current;
    const gen = generation.current, owner = identity;
    const live = () => generation.current === gen && identityRef.current === owner;
    const controller = new AbortController(); abort.current = controller;
    update({ ...current.current, loading: true });
    const timeout = setTimeout(() => controller.abort(), 15000);
    const run = async () => {
      try {
        const cursor = current.current.history.cursor;
        const url = `/api/puppet/${encodeURIComponent(target)}/history${cursor === null ? '' : `?cursor=${encodeURIComponent(cursor)}`}`;
        const page = await hybridRequest<HistoryPage>(url, undefined, controller.signal);
        if (!live()) return;
        if (!validPage(page)) throw new Error('invalid history response');
        const history = mergeHistory(current.current.history, page);
        update({ history, loading: false, error: null, hasMore: page.has_more, denied: false });
      } catch (error) {
        if (!live()) return;
        if (error instanceof HybridHttpError && [401, 403].includes(error.status)) {
          blocked.current = true;
          update({ ...initial, error: reasonOf(error), denied: true });
        } else update({ ...current.current, loading: false, error: reasonOf(error) });
      } finally { clearTimeout(timeout); if (live()) pending.current = null; }
    };
    const ride = run(); pending.current = ride;
    return ride;
  }, [enabled, session.role, session.login, target, identity, update]);
  useEffect(() => {
    generation.current++; blocked.current = false; pending.current = null;
    update(initial);
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (!active || !enabled || session.role !== 'admin' || blocked.current) return;
      await load();
      if (active && !blocked.current) timer = setTimeout(poll, current.current.hasMore ? 50 : 3000);
    };
    void poll();
    return () => { active = false; generation.current++; clearTimeout(timer); abort.current?.abort(); pending.current = null; };
  }, [identity, enabled, session.role, load, update]);
  return { ...view.history, error: view.error, loading: view.loading, hasMore: view.hasMore, denied: view.denied, load };
}
