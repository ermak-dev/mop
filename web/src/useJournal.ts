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
  // Поколение запросов -- по монотонной версии принятого (ревью 3):
  // решает не «кто начат последним», а «кто новее последнего принятого».
  // Опрос медленнее периода тогда не запирает журнал навечно (каждый
  // ответ новее принятого), а поздний 200 после свежего 403 всё равно
  // отбрасывается -- у 403 уже есть принятая версия.
  const accepted = useRef(0);
  // Порядковый номер запроса: растёт с каждым запуском, ответы сравниваются
  // с принятой версией -- «новее принятого», а не «начат последним».
  const seq = useRef(0);
  // Ключ источника: чей журнал в состоянии. Смена логина/роли/страницы --
  // другой источник: первый рендер после смены уже не показывает записей
  // прошлого (эффект ниже несёт новые). prev НЕ пишется в рендере после
  // чтения -- только эффектом, иначе «прошлый» всегда совпадал бы с
  // сегодняшним.
  const source = `${session.login ?? ""}\0${session.role ?? ""}\0${active}`;
  const prev = useRef(source);
  const stale = prev.current !== source;

  useEffect(() => {
    // не админ или страница закрыта: источник не спрашивают вовсе.
    // Отказ для закрытой страницы хук всё равно выводит: открытая руками
    // #journal без прав увидит причину, а не пустую таблицу.
    if (session.role !== "admin" || !active) {
      prev.current = source;
      setState(active ? REFUSED : { journal: [], error: null });
      return;
    }
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setInterval> | null = null;
    let alive = true;                 // эффект ушёл -- ни setState, ни парс

    const refused = (why: string) => setState({ journal: [], error: why });
    const load = () => {
      // версия этого запроса -- следующая за последним принятым
      const at = ++seq.current;
      fetch("/api/journal", { credentials: "same-origin", signal: ctrl.signal })
        .then(async (r) => {
          if (!alive || at <= accepted.current) return;
          if (r.ok) {
            const body = (await r.json()) as { journal: JournalEntry[] };
            // версия проверяется и после парса: тело старого ответа не
            // имеет права вернуть журнал после logout или свежего 403
            if (!alive || at <= accepted.current) return;
            accepted.current = at;
            setState({ journal: body.journal, error: null });
          } else {
            accepted.current = at;   // отказ -- тоже принятый ответ
            // 401 -- сессия кончилась, 403 -- роль ушла
            refused(r.status === 401 || r.status === 403 ? DENIED : SILENT);
          }
        })
        .catch(() => {
          if (!alive || at <= accepted.current) return;
          accepted.current = at;     // терминальный отказ держит барьер: поздний
                                     // 200 старого запроса не «вылечит» ошибку
          refused(SILENT);
        });
    };

    // смена admin A → admin B: прежние записи стёрты до нового ответа --
    // журнал не показывает чужого глаза прошлую картину; планка принятия
    // выше номеров прошлых запросов (их эффект поднял в cleanup)
    prev.current = source;
    setState({ journal: [], error: null });
    load();
    timer = setInterval(load, POLL_S * 1000);
    return () => {
      alive = false;
      // незакрытые запросы эффекта умирают: планка принятия -- выше любого
      // номера, который они могли получить (seq уже вырос на их стартах)
      accepted.current = seq.current;
      ctrl.abort();
      if (timer) clearInterval(timer);
    };
    // login в зависимостях: смена админа -- новый источник состояния
  }, [session.login, session.role, active, source]);

  // Синхронный возврат без эффекта: право ушло или источник сменился --
  // этот же рендер уже без записей прошлого (эффект выше донесёт новое,
  // но кадр не зависит от него).
  if (active && session.role !== "admin") return REFUSED;
  if (stale) return { journal: [], error: null };
  return state;
}
