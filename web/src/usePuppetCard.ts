// Приватные чтения карточки папета (#463, контракт #462): GET
// /api/puppet/<имя> и .../tail|/stderr. Только admin и только по явной цели:
// ни опроса, ни хранения -- записи живут в состоянии компонента, смена
// источника (роль, цель) стирает их в тот же рендер, а опоздавший ответ
// прежнего запроса не воскресит их. Поколение -- как в useJournal: принят
// бывает только ответ, начатый позже последнего принятого.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Puppet } from "./types";
import type { Session } from "./useSession";

export interface PuppetCardState { card: Puppet | null; error: string | null }

const DENIED = "карточка недоступна: прав не хватает";
const SILENT = "карточка недоступна: панель не отвечает";
const NO_SUCH = "нет такого папета";

const words: Record<number, string> = { 401: DENIED, 403: DENIED, 404: NO_SUCH };

export const refuseText = (status: number): string => words[status] ?? SILENT;

/** Карточка -- строка ростера по имени (#462). Цель нет -- спрашивать
 *  нечего; не-admin -- синхронный отказ без запроса. */
export function usePuppetCard(session: Session, target: string | null): PuppetCardState {
  const [state, setState] = useState<PuppetCardState>({ card: null, error: null });
  const accepted = useRef(0);
  const seq = useRef(0);
  // источник состояния: чья карточка. Смена -- первый рендер уже без
  // прежних фактов; prev пишется только эффектом (как useJournal).
  const source = `${session.login ?? ""}\0${session.role ?? ""}\0${target ?? ""}`;
  const prev = useRef(source);
  const stale = prev.current !== source;

  useEffect(() => {
    if (session.role !== "admin" || !target) {
      prev.current = source;
      setState(session.role === "admin" || !target
        ? { card: null, error: null }
        : { card: null, error: DENIED });
      return;
    }
    const ctrl = new AbortController();
    let alive = true;
    const at = ++seq.current;
    prev.current = source;
    setState({ card: null, error: null });
    fetch(`/api/puppet/${encodeURIComponent(target)}`,
          { credentials: "same-origin", signal: ctrl.signal })
      .then(async (r) => {
        if (!alive || at <= accepted.current) return;
        if (r.ok) {
          const body = (await r.json()) as { puppet: Puppet };
          if (!alive || at <= accepted.current) return;
          accepted.current = at;
          setState({ card: body.puppet, error: null });
        } else {
          accepted.current = at;
          setState({ card: null, error: refuseText(r.status) });
        }
      })
      .catch(() => {
        if (!alive || at <= accepted.current) return;
        accepted.current = at;
        setState({ card: null, error: SILENT });
      });
    return () => {
      alive = false;
      accepted.current = seq.current;
      ctrl.abort();
    };
    // target в зависимостях: смена цели -- другой источник
  }, [session.login, session.role, target, source]);

  if (session.role !== "admin" && target) return { card: null, error: DENIED };
  if (stale) return { card: null, error: null };
  return state;
}

export interface PuppetTailState {
  lines: string[];
  truncated: boolean;
  error: string | null;
  load: () => Promise<void>;
}

/** Хвост экрана (tail) или stderr задачи (stderr) -- по явному load(),
 *  без follow и таймеров: каждый кадр -- новое нажатие. Смена цели или
 *  потеря роли стирают строки; ответ не последнего load() отбрасывается. */
export function usePuppetTail(session: Session, target: string | null,
                              which: "tail" | "stderr" = "tail"): PuppetTailState {
  const [lines, setLines] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // поколение: растёт на каждый load и на каждую смену источника -- ответ
  // с меньшим номером молча теряется, чьи бы строки он ни вёз
  const gen = useRef(0);
  const source = `${session.login ?? ""}\0${session.role ?? ""}\0${target ?? ""}\0${which}`;
  const prev = useRef(source);

  // смена источника: стирание в тот же рендер, не эффектом после paint
  if (prev.current !== source) {
    prev.current = source;
    gen.current++;
    if (lines.length || truncated || error) {
      setLines([]);
      setTruncated(false);
      setError(null);
    }
  }

  const load = useCallback(async () => {
    if (session.role !== "admin" || !target) {
      gen.current++;
      setLines([]);
      setTruncated(false);
      setError(session.role !== "admin" && target ? DENIED : null);
      return;
    }
    const at = ++gen.current;
    try {
      const r = await fetch(`/api/puppet/${encodeURIComponent(target)}/${which}`,
                            { credentials: "same-origin" });
      if (at !== gen.current) return;
      if (!r.ok) { setLines([]); setTruncated(false); setError(refuseText(r.status)); return; }
      const body = (await r.json()) as { lines: string[]; truncated?: boolean };
      if (at !== gen.current) return;
      setLines(Array.isArray(body.lines) ? body.lines : []);
      setTruncated(body.truncated === true);
      setError(null);
    } catch {
      if (at !== gen.current) return;
      setLines([]);
      setTruncated(false);
      setError(SILENT);
    }
  }, [session.role, session.login, target, which]);

  return { lines, truncated, error, load };
}
