// Хук хранит выбор request_id и локальный отказ отправки, не копию Operation.
// Подтверждённый статус читается из того же reducer, что REST/WS и таблица.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Session } from './useSession';
import { HybridHttpError, reasonOf } from './hybridApi';
import { poolNetwork, sessionKey, usePoolState } from './poolNetwork';
export interface AdminOpState { status: string; id: string | null; error: string | null; lost: boolean }
export const TERMINAL = new Set(['succeeded', 'refused', 'failed', 'partial', 'unknown', 'cancelled', 'expired']);
const idle: AdminOpState = { status: 'idle', id: null, error: null, lost: false };
export function useAdminOp(session: Session, target: string | null) {
  const state = usePoolState();
  const source = `${sessionKey(session)}\0${target ?? ''}`;
  const [selection, setSelection] = useState<{ source: string; requestId: string; local: AdminOpState } | null>(null);
  const inflight = useRef<{ source: string; promise: Promise<string | null> } | null>(null);
  const currentSource = useRef(source); currentSource.current = source;
  const selected = selection?.source === source ? selection : null;
  const confirmed = selected && poolNetwork.matches(session) ? poolNetwork.operation(selected.requestId) : undefined;
  const fresh: AdminOpState = confirmed ? { status: confirmed.operation!.status, id: confirmed.id,
    error: typeof confirmed.data.reason === 'string' && confirmed.data.reason ? confirmed.data.reason : null, lost: false }
    : selected?.local ?? idle;
  // Один снимок исхода -- один объект: потребители (балуны) различают попытки
  // по идентичности, и перерисовка не должна выглядеть новым исходом.
  const op = useMemo(() => fresh,
    [selected?.requestId, fresh.status, fresh.id, fresh.error, fresh.lost]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (selected && poolNetwork.matches(session) && (selected.local.status === 'submitted' || selected.local.lost)) {
      poolNetwork.track(selected.requestId);
      void poolNetwork.recover();
    }
  }, [state.generation, source]);
  const submit = useCallback(async (action = 'restart', params: Record<string, unknown> = {}): Promise<string | null> => {
    if (inflight.current?.source === source) return inflight.current.promise;
    // Потерянный первый ответ — прежний request_id, только GET, не новый эффект.
    if (selected && (!TERMINAL.has(op.status) || op.lost)) { await poolNetwork.recover(); return poolNetwork.operation(selected.requestId)?.id ?? op.id; }
    const requestId = crypto.randomUUID();
    const local = { status: 'submitted', id: null, error: null, lost: false };
    setSelection({ source, requestId, local });
    const promise = (async () => {
      try {
        if (!target || !poolNetwork.matches(session)) throw new Error('Authenticated target required');
        return await poolNetwork.command(target, action, params, requestId);
      } catch (error) {
        const refused = error instanceof HybridHttpError ? error.status >= 400 && error.status < 500
          : !poolNetwork.state.pending[requestId];
        if (currentSource.current === source) setSelection({ source, requestId, local: {
          status: refused ? 'refused' : 'unknown', id: null, error: reasonOf(error), lost: !refused } });
        return null;
      } finally { if (inflight.current?.source === source) inflight.current = null; }
    })();
    inflight.current = { source, promise }; return promise;
  }, [source, target, selected, op.status, op.id, op.lost, session]);
  const forget = useCallback(() => { setSelection(null); }, []);
  return { op, submit, forget };
}
