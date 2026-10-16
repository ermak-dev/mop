// Карточка читает тот же versioned Entity, что таблица и команды (#627).
// target — стабильный ID, не runtime name. HTTP/detail принадлежит poolNetwork.
// Отдельный tail ниже пока относится к прежнему private workspace; он не
// участвует в REST/WS store и не является fallback источником pool entities.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Puppet } from "./types";
import type { Session } from "./useSession";
import { poolNetwork, presentPuppet, sessionKey, usePoolState } from './poolNetwork';
import { entityKey } from './poolStore';

export interface PuppetCardState { card: Puppet | null; error: string | null }

const DENIED = "карточка недоступна: прав не хватает";
const SILENT = "карточка недоступна: панель не отвечает";
const NO_SUCH = "нет такого папета";

const words: Record<number, string> = { 401: DENIED, 403: DENIED, 404: NO_SUCH };

export const refuseText = (status: number): string => words[status] ?? SILENT;

/** Без отдельной копии подтверждённых фактов в компоненте. */
export function usePuppetCard(session: Session, target: string | null): PuppetCardState {
  const state = usePoolState();
  const key = sessionKey(session);
  useEffect(() => {
    if (target && poolNetwork.matches(session)) return poolNetwork.watchDetail(target);
  }, [key, target]);
  if (!target || !poolNetwork.matches(session)) return { card: null, error: null };
  const error = poolNetwork.detailError(session, target);
  const row = poolNetwork.puppet(target);
  return { card: row && !error ? presentPuppet(row, Date.now(), state.entities[entityKey(row)]?.invalidated) : null, error };
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
