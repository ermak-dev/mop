// Операция админа (#465): POST /api/admin/ops {action,target,params} ->
// 202 {id} -> один GET сразу после 202 (мгновенные refused, waiting/
// running на момент подачи), дальше терминал приезжает кадром SSE
// `operation` (#504 useOperationFeed -- канал держит App и раздаёт
// операциям по id); потерянный терминал покрывает таймер канала
// (серверный потолок эффекта), не эта петля. Модель статусов -- #476:
// queued/running/accepted -- ждём, succeeded/refused/failed/partial/
// unknown -- терминал; unknown НЕ failed (эффекта могли и не быть),
// автоповтора нет -- повтор только решением админа. Идемпотентность --
// сервер (#461): тот же payload -- тот же id; клиент не плодит второй
// POST: двойной под и повтор при живой задаче возвращают тот же id.
// Смена источника (роль, цель) стирает операцию в тот же рендер; 202,
// опоздавший за сменой, не начинается вовсе. Потерянный первый ответ
// (сбой сети, 202 без id) -- unknown без id: ни эффекта, ни отказа,
// автоповтора тем более (#504: без id нет ни операции, ни таймера).
// Результат живёт в памяти страницы (#465, первый выпуск): перезагрузка
// начинает заново.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Session } from "./useSession";
import { operations } from "./useOperationFeed";

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

/** Слова отказов страницы (#463 -- тот же словарь слов, не второй). */
const DENIED = "операция недоступна: прав не хватает";

export function useAdminOp(session: Session, target: string | null) {
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
    // Цель у add нет (#472): контракт {action, target?, params}; отказ
    // только у не-admin -- null-цель сама по себе не отказ.
    if (session.role !== "admin") {
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
        // Ключ target при null-цели не едет вовсе (#472): контракт
        // {action, target?, params}, и null в JSON не «отсутствие».
        const schema = target === null
          ? { action: act, params }
          : { action: act, target, params };
        const r = await fetch("/api/admin/ops", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(schema),
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

  // «Новая команда» (#522): визард просит забыть показанный вердикт --
  // стейт в idle тем же рендером, поздние кадры старого id глохнут по
  // seq. Живой ход не забывается: forget зовёт только терминальный футер.
  const forget = useCallback(() => {
    seq.current += 1;
    inflight.current = null;
    setOp({ status: "idle", id: null, error: null, lost: false });
  }, []);

  // Единственный GET после 202 (#504): ловит мгновенный refused и статусы
  // на момент подачи (waiting/running); дальше терминал ждут кадром.
  // Зависимость -- только id: повторный рендер со сменой промежуточного
  // статуса не рождает второй GET. «not found» -- запись вытекла: правда
  // -- журнал, это unknown.
  useEffect(() => {
    if (!op.id || TERMINAL.has(op.status)) return;
    let alive = true;
    const at = op.id;
    operations.accepted(target, at);
    (async () => {
      try {
        const r = await fetch(`/api/admin/ops/${at}`,
                              { credentials: "same-origin" });
        if (!alive || !r.ok) return;
        const body = (await r.json()) as { status?: string; error?: string };
        if (!alive) return;
        const status = body.status ?? "";
        if (status === "not found") {
          // Запись вытекла/очищена: ни эффекта, ни отказа не известно.
          operations.expired(target, at);
          setOp((o) => o.id === at ? { status: "unknown", id: at, error: null, lost: false } : o);
        } else if (TERMINAL.has(status)) {
          operations.observed(target, at, status);
          setOp((o) => o.id === at
            ? { status, id: at, error: body.error ?? null, lost: false } : o);
        } else if (status) {
          operations.observed(target, at, status);
          setOp((o) => o.id === at && !TERMINAL.has(o.status)
            ? { ...o, status } : o);
        }
      } catch { /* GET не прошёл -- терминал ждём кадром (#504) */ }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [op.id]);

  // Терминальный кадр канала (#504): своя операция по id доходит до
  // вердикта без всякого опроса. Чужой id молчит.
  useEffect(() => operations.subscribe((f) => {
    if (!TERMINAL.has(f.status)) return;
    setOp((o) => o.id === f.id && !TERMINAL.has(o.status)
      ? { status: f.status, id: o.id, error: f.error ?? null, lost: false }
      : o);
  }), []);

  return { op, submit, forget };
}

/** Фраза отказа -- allowlist-ответ сервера, без сырых исключений. */
async function phrase(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { error?: string };
    if (body.error) return body.error;
  } catch { /* нет тела -- общая фраза */ }
  return `операция не принята: ${r.status}`;
}
