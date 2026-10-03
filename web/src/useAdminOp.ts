// Операция админа (#465): POST /api/admin/ops {action,target,params} ->
// 202 {id} -> опрос GET /api/admin/ops/<id> до терминала. Модель статусов
// -- #476: queued/running/accepted -- ждём, succeeded/refused/failed/
// partial/unknown -- терминал; unknown НЕ failed (эффекта могли и не
// быть), автоповтора нет -- повтор только решением админа. Идемпотентность
// -- сервер (#461): тот же payload -- тот же id; клиент не плодит второй
// POST: двойной под и повтор при живой задаче возвращают тот же id.
// Смена источника (роль, цель) стирает операцию в тот же рендер; 202,
// опоздавший за сменой, не начинает опрос. Потерянный первый ответ
// (сбой сети, 202 без id) -- unknown без id: ни эффекта, ни отказа,
// автоповтора тем более. Результат живёт в памяти страницы (#465,
// первый выпуск): перезагрузка начинает заново.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Session } from "./useSession";

export interface AdminOpState {
  /** idle -> submitted -> accepted -> queued/running -> терминал/unknown. */
  status: string;
  /** id задачи или null (потерянный ответ, ещё не отвечено, отказ). */
  id: string | null;
  /** allowlist-фраза отказа, null -- нет отказа. */
  error: string | null;
  /** true -- первый ответ не долетел: исход операции неизвестен. */
  lost: boolean;
}

/** Терминальные статусы (#476). unknown -- терминал, но НЕ failed. */
export const TERMINAL = new Set(["succeeded", "refused", "failed", "partial", "unknown"]);
const POLL_MS = 1000;

/** Слова отказов страницы (#463 -- тот же словарь слов, не второй). */
const DENIED = "операция недоступна: прав не хватает";

export function useAdminOp(session: Session, target: string | null,
                           pollMs = POLL_MS) {
  const [op, setOp] = useState<AdminOpState>(
    { status: "idle", id: null, error: null, lost: false });
  const seq = useRef(0);
  // общий полёт: двойной под -- тот же промис, второй POST не рождается
  const inflight = useRef<Promise<string | null> | null>(null);
  // источник операции: чья. prev пишется только эффектом, как в useJournal
  // и usePuppetCard: первый рендер после смены уже без прежней записи.
  const source = `${session.login ?? ""}\0${session.role ?? ""}\0${target ?? ""}`;
  const prev = useRef(source);
  const stale = prev.current !== source;

  useEffect(() => {
    if (stale) {
      prev.current = source;
      seq.current += 1;
      inflight.current = null;
      setOp({ status: "idle", id: null, error: null, lost: false });
    }
  }, [source, stale]);

  const submit = useCallback(async (act: string = "restart",
                                params: Record<string, unknown> = {}):
    Promise<string | null> => {
    if (session.role !== "admin" || !target) {
      setOp({ status: "refused", id: null, error: DENIED, lost: false });
      return null;
    }
    // Уже в полёте: тот же ответ, без второго POST.
    if (inflight.current) return inflight.current;
    // Принята и ещё не терминальна: id уже есть, сервер повтор бы дедупнул,
    // но и запрос не нужен.
    if (op.id && !TERMINAL.has(op.status)) return op.id;
    const at = ++seq.current;
    setOp({ status: "submitted", id: null, error: null, lost: false });
    const ride = (async (): Promise<string | null> => {
      try {
        const r = await fetch("/api/admin/ops", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: act, target, params }),
        });
        if (at !== seq.current) return null;      // источник сменился
        if (!r.ok) {
          setOp({ status: "refused", id: null, error: await phrase(r), lost: false });
          return null;
        }
        const body = (await r.json()) as { id?: string };
        if (at !== seq.current) return null;
        if (!body.id) {
          // 202 без id: ответ потерян -- исход операции неизвестен.
          setOp({ status: "unknown", id: null, error: null, lost: true });
          return null;
        }
        setOp({ status: "accepted", id: body.id, error: null, lost: false });
        return body.id;
      } catch {
        if (at !== seq.current) return null;
        setOp({ status: "unknown", id: null, error: null, lost: true });
        return null;
      } finally {
        if (inflight.current === ride) inflight.current = null;
      }
    })();
    inflight.current = ride;
    return ride;
  }, [session.role, target, op.id, op.status]);

  // Опрос результата: только пока есть id и статус не терминальный.
  // Первый GET -- сразу: 202 ещё ничего не значил, и бессмысленно ждать
  // интервал до первой правды.
  useEffect(() => {
    if (!op.id || TERMINAL.has(op.status)) return;
    let alive = true;
    let misses = 0;
    const tick = async () => {
      while (alive) {
        try {
          const r = await fetch(`/api/admin/ops/${op.id}`,
                                { credentials: "same-origin" });
          if (!alive) return;
          if (!r.ok) {
            misses += 1;
            if (misses >= 3) {
              setOp({ status: "unknown", id: op.id, error: null, lost: false });
              return;
            }
          } else {
            misses = 0;
            const body = (await r.json()) as { status?: string; error?: string };
            if (!alive) return;
            const status = body.status ?? "";
            if (status === "not found") {
              // Запись вытекла/очищена: ни эффекта, ни отказа не известно.
              setOp({ status: "unknown", id: op.id, error: null, lost: false });
              return;
            }
            if (TERMINAL.has(status)) {
              setOp({ status, id: op.id, error: body.error ?? null, lost: false });
              return;
            }
            if (status) setOp((o) => ({ ...o, status }));
          }
        } catch {
          misses += 1;
          if (misses >= 3) {
            if (alive) setOp({ status: "unknown", id: op.id, error: null, lost: false });
            return;
          }
        }
        await new Promise((res) => setTimeout(res, pollMs));
      }
    };
    void tick();
    return () => { alive = false; };
  }, [op.id, op.status, pollMs]);

  return { op, submit };
}

/** Фраза отказа -- allowlist-ответ сервера, без сырых исключений. */
async function phrase(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { error?: string };
    if (body.error) return body.error;
  } catch { /* нет тела -- общая фраза */ }
  return `операция не принята: ${r.status}`;
}
