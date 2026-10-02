// Журнал админа (#422): GET /api/journal (#424) при admin-сессии -- только
// когда страница журнала открыта, и с ограниченным опросом: SSE публичен и
// журнала больше не несёт (#424), а держать вкладку открытой -- обычный
// режим админа. Записи живут только в состоянии этой страницы -- ни
// storage, ни кэша.
//
// Отказ выведен синхронно (ревью 2): role !== "admin" или закрытая
// страница -- тот же рендер возвращает пустой журнал с отказом, эффект
// ничего не дочищает: ни одного кадра прежних данных после разжалования.
// Запросы несут поколение: ответ вирен только пока он -- последний
// начатый; опоздавший 200 прошлого круга не восстановит события после
// свежего 403.
import { useEffect, useRef, useState } from "react";
import type { JournalEntry } from "./types";
import type { Session } from "./useSession";

export interface Journal { journal: JournalEntry[]; error: string | null }

/** Период опроса открытой страницы журнала, с -- как POLL_MS usePool. */
export const POLL_S = 10;

const DENIED = "журнал недоступен: прав не хватает";
const SILENT = "журнал недоступен: панель не отвечает";
const REFUSED: Journal = { journal: [], error: DENIED };

export function useJournal(session: Session, active: boolean): Journal {
  const [state, setState] = useState<Journal>({ journal: [], error: null });
  // Поколение запросов: решает последний начатый. Живёт в ref -- эффект
  // не перезапускается от него, а ответы сравниваются с текущим.
  const gen = useRef(0);

  useEffect(() => {
    // не админ или страница закрыта: источник не спрашивают вовсе.
    // Отказ для закрытой страницы хук всё равно выводит: открытая руками
    // #journal без прав увидит причину, а не пустую таблицу.
    if (session.role !== "admin" || !active) {
      setState(active ? REFUSED : { journal: [], error: null });
      return;
    }
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setInterval> | null = null;
    let alive = true;                 // эффект ушёл -- ни setState, ни парс

    const refused = (why: string) => setState({ journal: [], error: why });
    const load = () => {
      // смена поколения до ответа: прошлый запуск больше не решает
      const at = ++gen.current;
      fetch("/api/journal", { credentials: "same-origin", signal: ctrl.signal })
        .then(async (r) => {
          if (!alive || at !== gen.current) return;
          if (r.ok) {
            const body = (await r.json()) as { journal: JournalEntry[] };
            // поколение проверяется и после парса: тело старого ответа не
            // имеет права вернуть журнал после logout или свежего 403
            if (!alive || at !== gen.current) return;
            setState({ journal: body.journal, error: null });
          } else {
            // 401 -- сессия кончилась, 403 -- роль ушла
            refused(r.status === 401 || r.status === 403 ? DENIED : SILENT);
          }
        })
        .catch(() => { if (alive && at === gen.current) refused(SILENT); });
    };

    // смена admin A → admin B: прежние записи стёрты до нового ответа --
    // журнал не показывает чужого глаза прошлую картину
    setState({ journal: [], error: null });
    load();
    timer = setInterval(load, POLL_S * 1000);
    return () => {
      alive = false;
      gen.current++;                  // ответы прошлого эффекта -- в никуда
      ctrl.abort();
      if (timer) clearInterval(timer);
    };
    // login в зависимостях: смена админа -- новый источник состояния
  }, [session.login, session.role, active]);

  // Синхронный отказ без эффекта: роль ушла -- этот же рендер уже без
  // записей (эффект выше дочистит состояние, но кадр не зависит от него)
  if (active && session.role !== "admin") return REFUSED;
  return state;
}
