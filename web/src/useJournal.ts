// Журнал админа (#422): GET /api/journal (#424) при admin-сессии -- только
// когда страница журнала открыта, и с ограниченным опросом: SSE публичен и
// журнала больше не несёт (#424), а держать вкладку открытой -- обычный
// режим админа. Записи живут только в состоянии этой страницы -- ни
// storage, ни кэша: право ушло (401/403, TTL, выход, разжалование) --
// записи стираются тем же ответом, что называет отказ.
import { useEffect, useState } from "react";
import type { JournalEntry } from "./types";
import type { Session } from "./useSession";

export interface Journal { journal: JournalEntry[]; error: string | null }

/** Период опроса открытой страницы журнала, с -- как POLL_MS usePool. */
export const POLL_S = 10;

const DENIED = "журнал недоступен: прав не хватает";
const SILENT = "журнал недоступен: панель не отвечает";

export function useJournal(session: Session, active: boolean): Journal {
  const [journal, setJournal] = useState<JournalEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // не админ или страница закрыта: прежних записей в состоянии не
    // остаётся, и источник не спрашивают вовсе -- журнал виден только
    // действующему админу, и только пока он его смотрит
    if (session.role !== "admin" || !active) {
      setJournal([]);
      setError(null);
      return;
    }
    const ctrl = new AbortController();
    let alive = true;          // эффект ушёл -- ни setState, ни парс тела
    let timer: ReturnType<typeof setInterval> | null = null;

    const refused = (why: string) => { setJournal([]); setError(why); };
    const load = () => {
      fetch("/api/journal", { credentials: "same-origin", signal: ctrl.signal })
        .then(async (r) => {
          if (!alive) return;
          if (r.ok) {
            const body = (await r.json()) as { journal: JournalEntry[] };
            // alive и после парса: тело старого ответа не имеет права
            // вернуть журнал уже вышедшему админу
            if (!alive) return;
            setJournal(body.journal);
            setError(null);
          } else {
            // 401 -- сессия кончилась, 403 -- роль ушла
            refused(r.status === 401 || r.status === 403 ? DENIED : SILENT);
          }
        })
        .catch(() => { if (alive) refused(SILENT); });
    };

    setError(null);
    load();
    timer = setInterval(load, POLL_S * 1000);
    return () => {
      alive = false;
      ctrl.abort();
      if (timer) clearInterval(timer);
    };
  }, [session.login, session.role, active]);

  return { journal, error };
}
