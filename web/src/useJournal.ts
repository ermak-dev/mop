// Журнал админа (#422): GET /api/journal (#424) при admin-сессии. Записи
// живут только в состоянии этой страницы -- ни storage, ни кэша: право
// ушло (401/403, TTL, выход, разжалование) -- записи стираются тем же
// рендером, что рисует отказ. Тянет только админ: гость и user не
// спрашивают вовсе, их вкладка не показывает журнала.
import { useEffect, useState } from "react";
import type { JournalEntry } from "./types";
import type { Session } from "./useSession";

export interface Journal { journal: JournalEntry[]; error: string | null }

export function useJournal(session: Session): Journal {
  const [journal, setJournal] = useState<JournalEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (session.role !== "admin") {
      // не админ: прежних записей в состоянии не остаётся -- журнал виден
      // только действующему админу; вместо пустой таблицы -- отказ, чтобы
      // открытая руками #journal не выглядела «событий не было»
      setJournal([]);
      setError("журнал недоступен: прав не хватает");
      return;
    }
    let alive = true;
    setJournal([]);
    fetch("/api/journal", { credentials: "same-origin" })
      .then(async (r) => {
        if (!alive) return;
        if (r.ok) {
          setJournal(((await r.json()) as { journal: JournalEntry[] }).journal);
          setError(null);
        } else {
          // 401 -- сессия кончилась, 403 -- роль ушла: то и другое --
          // «прав не хватает», записей нет
          setJournal([]);
          setError(r.status === 401 || r.status === 403
            ? "журнал недоступен: прав не хватает" : "журнал недоступен: панель не отвечает");
        }
      })
      .catch(() => { if (alive) { setJournal([]); setError("журнал недоступен: панель не отвечает"); } });
    return () => { alive = false; };
  }, [session.login, session.role]);

  return { journal, error };
}
