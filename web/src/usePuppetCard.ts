// Приватные чтения карточки папета (#463, контракт #462): GET
// /api/puppet/<имя> и .../tail (#507: чтение stderr ушло вместе с кнопкой
// «лог»). Только admin и только по явной цели:
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
  mode: "screen" | "initialization" | null;
  note: string | null;
  truncated: boolean;
  error: string | null;
  loading: boolean;
  loaded: boolean;
  blocked: boolean;
  load: () => Promise<void>;
}

/** Чтение кадра без встроенного таймера: расписанием управляет просмотрщик.
 *  Временный отказ сохраняет последний кадр; потеря прав, цели или роли
 *  стирает его. Ответ не последнего load() отбрасывается. */
export function usePuppetTail(session: Session, target: string | null): PuppetTailState {
  const [lines, setLines] = useState<string[]>([]);
  const [mode, setMode] = useState<"screen" | "initialization" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const requests = useRef(new Set<AbortController>());
  // поколение: растёт на каждый load и на каждую смену источника -- ответ
  // с меньшим номером молча теряется, чьи бы строки он ни вёз
  const gen = useRef(0);
  const source = `${session.login ?? ""}\0${session.role ?? ""}\0${target ?? ""}`;
  const prev = useRef(source);

  // смена источника: стирание в тот же рендер, не эффектом после paint
  if (prev.current !== source) {
    prev.current = source;
    gen.current++;
    setLines([]);
    setMode(null);
    setNote(null);
    setTruncated(false);
    setError(null);
    setLoading(false);
    setLoaded(false);
    setBlocked(false);
  }

  useEffect(() => () => {
    gen.current++;
    for (const controller of requests.current) controller.abort();
    requests.current.clear();
  }, [source]);

  const load = useCallback(async () => {
    if (session.role !== "admin" || !target) {
      gen.current++;
      setLines([]);
      setMode(null);
      setNote(null);
      setTruncated(false);
      setError(session.role !== "admin" && target ? DENIED : null);
      setLoading(false);
      setLoaded(false);
      return;
    }
    const at = ++gen.current;
    const controller = new AbortController();
    requests.current.add(controller);
    setLoading(true);
    try {
      const r = await fetch(`/api/puppet/${encodeURIComponent(target)}/tail`,
                            { credentials: "same-origin", signal: controller.signal });
      if (at !== gen.current) return;
      if (!r.ok) {
        if ([401, 403, 404].includes(r.status)) {
          setLines([]);
          setMode(null);
          setNote(null);
          setTruncated(false);
          setBlocked(true);
        }
        let reason = refuseText(r.status);
        if (![401, 403, 404].includes(r.status)) {
          try {
            const body = await r.json() as { source?: string; error?: string };
            if (at !== gen.current) return;
            if (body.source === "initialization" && typeof body.error === "string") {
              reason = body.error;
              setMode("initialization");
            }
          } catch { /* Ответ шлюза может не быть JSON. */ }
        }
        if (at !== gen.current) return;
        setError(reason);
        return;
      }
      const body = (await r.json()) as { lines: string[]; truncated?: boolean;
        source?: "screen" | "initialization"; note?: string | null };
      if (at !== gen.current) return;
      if (body.source !== undefined && body.source !== "screen" && body.source !== "initialization") {
        setMode(null);
        setError("источник вывода неизвестен");
        return;
      }
      const next = Array.isArray(body.lines) ? body.lines : [];
      setLines((previous) => previous.length === next.length
        && previous.every((line, i) => line === next[i]) ? previous : next);
      setTruncated(body.truncated === true);
      setMode(body.source === "initialization" ? "initialization" : "screen");
      setNote(typeof body.note === "string" ? body.note : null);
      setError(null);
      setBlocked(false);
    } catch {
      if (at !== gen.current) return;
      setError(SILENT);
    } finally {
      requests.current.delete(controller);
      if (at === gen.current) {
        setLoading(false);
        setLoaded(true);
      }
    }
  }, [session.role, session.login, target]);

  return { lines, mode, note, truncated, error, loading, loaded, blocked, load };
}
