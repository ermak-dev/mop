// Каталог моделей для формы смены (#468): GET /api/admin/models --
// admin-only, поэтому список есть только у админа и только по ask().
// Один источник: id приходят с сервера (прокси -- единственный, кто знает
// каталог, #379), страница не выдумывает своего списка. Ответ читается
// один раз и живёт в памяти страницы: каталог не меняется каждые пол-
// секунды, обновление -- заново открытая форма. Смена источника (роль)
// стирает список в тот же рендер; в storage его нет (запрет #463).
import { useCallback, useEffect, useRef, useState } from "react";
import type { Session } from "./useSession";
import { refuseText } from "./usePuppetCard";

export interface ModelsState {
  models: string[];
  error: string | null;
  /** Загрузить каталог, если он ещё не в памяти. Повторные ask -- бесплатно. */
  ask: () => void;
}

export function useModels(session: Session, want: boolean): ModelsState {
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const asked = useRef(false);
  const seq = useRef(0);
  const source = `${session.login ?? ""}\0${session.role ?? ""}`;
  const prev = useRef(source);
  // смена источника: первый рендер уже без прежнего списка
  const stale = prev.current !== source;

  useEffect(() => {
    if (stale) {
      prev.current = source;
      asked.current = false;
      seq.current += 1;
      setModels([]);
      setError(null);
    }
  }, [source, stale]);

  const ask = useCallback(() => {
    if (asked.current) return;
    asked.current = true;
    if (session.role !== "admin") return;   // форме не-admin не видна вовсе
    const at = ++seq.current;
    fetch("/api/admin/models", { credentials: "same-origin" })
      .then(async (r) => {
        if (at !== seq.current) return;
        if (!r.ok) { setError(refuseText(r.status)); return; }
        const body = (await r.json()) as { models?: string[] };
        if (at !== seq.current) return;
        setModels(Array.isArray(body.models) ? body.models : []);
        setError(null);
      })
      .catch(() => {
        if (at !== seq.current) return;
        setError(refuseText(503));
      });
  }, [session.role, want]);

  // want от формы: открыли форму -- спросили
  useEffect(() => {
    if (want) ask();
  }, [want, ask]);

  if (stale) return { models: [], error: null, ask };
  return { models, error, ask };
}
