import { useCallback, useEffect, useRef, useState } from 'react';
import type { Session } from './useSession';
import { decodeOutput } from './hybridProtocol';
import { HybridHttpError, hybridRequest, reasonOf, terminalBase } from './hybridApi';
export interface TerminalSink { write: (data: Uint8Array) => void; reset: () => void }
interface Attach { tid: string; epoch: string; cursor: number; mode: 'ro' | 'rw'; cols: number; rows: number }
interface ReadFrame { data: string; cursor: number; reset: boolean; closed: boolean; exit: unknown; epoch: string; error?: string }
interface InputReply { written: number; seq: number; outcome: 'written' | 'duplicate' | 'refused' | 'unknown'; error?: string }
interface TerminalState { mode: 'ro' | 'rw' | null; phase: 'idle' | 'opening' | 'connected' | 'disconnected' | 'unknown'; error: string | null; busy: boolean }
const initial: TerminalState = { mode: null, phase: 'idle', error: null, busy: false };
const dimensions = (cols: number, rows: number) => ({ cols: Math.max(20, Math.min(500, Math.floor(cols))), rows: Math.max(5, Math.min(200, Math.floor(rows))) });

export function useHybridTerminal(session: Session, target: string, sink: TerminalSink) {
  const [state, setState] = useState<TerminalState>(initial);
  const stateRef = useRef(state);
  const sinkRef = useRef(sink); sinkRef.current = sink;
  const generation = useRef(0);
  const attachment = useRef<Attach | null>(null);
  const readAbort = useRef<AbortController | null>(null);
  const inputAbort = useRef<AbortController | null>(null);
  const opening = useRef(false);
  const seq = useRef(0);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const identity = `${session.login}\0${session.role}\0${target}`;
  const identityRef = useRef(identity); identityRef.current = identity;
  const base = terminalBase(target);
  const update = useCallback((next: TerminalState) => { stateRef.current = next; setState(next); }, []);
  const closeAt = useCallback(async (url: string, attach: Attach) => {
    const reply = await hybridRequest<{ ok: boolean; error?: string }>(`${url}/${encodeURIComponent(attach.tid)}/close`, {});
    if (reply.ok !== true) throw new Error(reply.error ?? 'attach close refused');
  }, []);
  const cancel = useCallback(() => {
    generation.current++;
    readAbort.current?.abort(); inputAbort.current?.abort();
    readAbort.current = null; inputAbort.current = null;
    opening.current = false;
    queue.current = Promise.resolve();
  }, []);

  useEffect(() => {
    update(initial);
    sinkRef.current.reset();
    return () => {
      cancel();
      const old = attachment.current; attachment.current = null;
      sinkRef.current.reset();
      // Cleanup закрывает только attach. Нет restart/kill папета и replay.
      if (old) void closeAt(base, old).catch(() => { /* UI уже размонтирован. */ });
    };
  }, [identity, base, cancel, closeAt, update]);

  const disconnect = useCallback((message: string, clear = false) => {
    cancel();
    const old = attachment.current, gen = generation.current;
    if (clear) sinkRef.current.reset();
    update({ mode: null, phase: 'disconnected', error: message, busy: false });
    if (old) void closeAt(base, old).then(() => {
      if (generation.current === gen && attachment.current === old) attachment.current = null;
    }).catch(error => {
      if (generation.current === gen && identityRef.current === identity) update({ mode: null, phase: 'disconnected', error: `${message}; close: ${reasonOf(error)}`, busy: false });
    });
  }, [cancel, closeAt, base, identity, update]);

  const readLoop = useCallback(async (attach: Attach, gen: number, owner: string) => {
    const live = () => generation.current === gen && identityRef.current === owner && attachment.current === attach;
    while (live()) {
      const controller = new AbortController(); readAbort.current = controller;
      // Long poll не удерживает очередь input; даже зависший read ограничен.
      const timeout = setTimeout(() => controller.abort(), 22000);
      let retry = false;
      try {
        const frame = await hybridRequest<ReadFrame>(`${base}/${encodeURIComponent(attach.tid)}/read?cursor=${attach.cursor}&wait=15`, undefined, controller.signal);
        if (!live()) return;
        if (frame.reset || frame.epoch !== attach.epoch || frame.closed || frame.error) {
          disconnect(frame.error ?? (frame.closed ? `терминал закрыт (exit: ${JSON.stringify(frame.exit)})` : 'буфер или сессия изменились; переподключитесь явно'), !!frame.reset || frame.epoch !== attach.epoch);
          return;
        }
        if (typeof frame.data !== 'string' || !Number.isSafeInteger(frame.cursor) || frame.cursor < attach.cursor || frame.reset !== false || frame.closed !== false) throw new Error('invalid terminal read response');
        if (frame.data && frame.cursor === attach.cursor) throw new Error('terminal cursor did not advance');
        sinkRef.current.write(decodeOutput(frame.data));
        attach.cursor = frame.cursor;
        if (stateRef.current.phase === 'connected' && stateRef.current.error) update({ ...stateRef.current, error: null });
      } catch (error) {
        if (!live()) return;
        if (error instanceof HybridHttpError && [401, 403].includes(error.status)) { disconnect(reasonOf(error), true); return; }
        if (attach.mode === 'rw') { disconnect(`чтение потеряно: ${reasonOf(error)}; переподключитесь явно`); return; }
        update({ ...stateRef.current, error: `чтение недоступно: ${reasonOf(error)}; повтор только чтения` });
        retry = true;
      } finally { clearTimeout(timeout); }
      // Пустой немедленный ответ не создаёт горячий цикл; retry не открывает writer.
      await new Promise<void>(resolve => {
        const timer = setTimeout(resolve, retry ? 1500 : 30);
        controller.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
        if (controller.signal.aborted) { clearTimeout(timer); resolve(); }
      });
    }
  }, [base, disconnect, update]);

  const open = useCallback(async (mode: 'ro' | 'rw', cols = 80, rows = 24) => {
    if (session.role !== 'admin' || !session.login || opening.current) return;
    cancel(); opening.current = true;
    const gen = generation.current, owner = identity;
    const live = () => generation.current === gen && identityRef.current === owner;
    update({ mode: null, phase: 'opening', error: null, busy: false });
    const old = attachment.current;
    try {
      if (old) await closeAt(base, old);
      if (!live()) return;
      attachment.current = null;
      const attach = await hybridRequest<Attach>(base, { mode, ...dimensions(cols, rows) });
      if (!live()) {
        if (typeof attach.tid === 'string') await closeAt(base, attach);
        return;
      }
      if (typeof attach.tid !== 'string' || !attach.tid || typeof attach.epoch !== 'string' || !Number.isSafeInteger(attach.cursor) || attach.cursor < 0 || attach.mode !== mode || !Number.isInteger(attach.cols) || !Number.isInteger(attach.rows)) {
        if (typeof attach.tid === 'string') await closeAt(base, attach);
        throw new Error('invalid terminal attach response');
      }
      attachment.current = attach; seq.current = 0;
      sinkRef.current.reset();
      update({ mode, phase: 'connected', error: null, busy: false });
      void readLoop(attach, gen, owner);
    } catch (error) {
      if (live()) {
        if (error instanceof HybridHttpError && [401, 403].includes(error.status)) sinkRef.current.reset();
        update({ mode: null, phase: 'disconnected', error: reasonOf(error), busy: false });
      }
    } finally { if (live()) opening.current = false; }
  }, [session.role, session.login, identity, base, cancel, closeAt, update, readLoop]);

  const send = useCallback((parts: string[]): Promise<boolean> => {
    const attach = attachment.current, gen = generation.current, owner = identity;
    const live = () => generation.current === gen && identityRef.current === owner && attachment.current === attach && stateRef.current.phase === 'connected';
    if (!attach || attach.mode !== 'rw' || !live()) return Promise.resolve(false);
    const operation = async () => {
      if (!live()) return false;
      update({ ...stateRef.current, busy: true });
      try {
        for (const data of parts) {
          if (!live()) return false;
          const size = atob(data).length;
          if (!size || size > 4096) throw new Error('invalid input size');
          const number = ++seq.current;
          const controller = new AbortController(); inputAbort.current = controller;
          let timeout: ReturnType<typeof setTimeout> | undefined;
          let reply: InputReply;
          try {
            reply = await Promise.race([
              hybridRequest<InputReply>(`${base}/${encodeURIComponent(attach.tid)}/input`, { seq: number, data }, controller.signal),
              new Promise<never>((_, reject) => { timeout = setTimeout(() => { controller.abort(); reject(new Error('input timeout; outcome unknown')); }, 8000); }),
            ]);
          } finally { clearTimeout(timeout); }
          if (!live()) return false;
          if (reply.seq !== number || !['written', 'duplicate'].includes(reply.outcome) || reply.written !== size) {
            update({ mode: 'rw', phase: reply.outcome === 'refused' ? 'disconnected' : 'unknown', error: reply.error ?? `input ${reply.outcome ?? 'unknown'}; переподключитесь явно, не повторяйте ввод вслепую`, busy: false });
            return false;
          }
        }
        return true;
      } catch (error) {
        if (live() && error instanceof HybridHttpError && [401, 403].includes(error.status)) disconnect(reasonOf(error), true);
        else if (live()) update({ mode: 'rw', phase: 'unknown', error: `результат ввода неизвестен: ${reasonOf(error)}; переподключитесь явно`, busy: false });
        return false;
      } finally { if (live()) update({ ...stateRef.current, busy: false }); }
    };
    const pending = queue.current.then(operation);
    queue.current = pending;
    return pending;
  }, [identity, base, update, disconnect]);

  const resize = useCallback(async (cols: number, rows: number) => {
    const attach = attachment.current, gen = generation.current, owner = identity;
    if (!attach || attach.mode !== 'rw' || stateRef.current.phase !== 'connected') return;
    try {
      const reply = await hybridRequest<{ ok: boolean; error?: string }>(`${base}/${encodeURIComponent(attach.tid)}/resize`, dimensions(cols, rows));
      if (!reply.ok) throw new Error(reply.error ?? 'resize refused');
    } catch (error) {
      if (gen === generation.current && identityRef.current === owner) disconnect(`resize: ${reasonOf(error)}`, error instanceof HybridHttpError && [401, 403].includes(error.status));
    }
  }, [identity, base, disconnect]);
  const close = useCallback(async () => {
    cancel();
    const gen = generation.current, old = attachment.current;
    update(initial);
    if (old) try {
      await closeAt(base, old);
      if (generation.current === gen && attachment.current === old) attachment.current = null;
    } catch (error) { if (generation.current === gen && identityRef.current === identity) update({ ...initial, phase: 'disconnected', error: reasonOf(error) }); }
  }, [cancel, closeAt, base, identity, update]);
  const invalidate = useCallback((epoch: string | null, sessionChanged = false) => {
    if (attachment.current && (sessionChanged || (epoch && epoch !== attachment.current.epoch))) disconnect('сессия изменилась; переподключитесь явно', true);
  }, [disconnect]);
  const revoke = useCallback(() => disconnect('авторизация потеряна; откройте сессию заново', true), [disconnect]);
  return { ...state, canInput: state.mode === 'rw' && state.phase === 'connected', open, send, resize, close, invalidate, revoke };
}
