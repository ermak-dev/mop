// Реестр проектов для формы add (#472): GET /api/admin/projects --
// admin-only, поэтому список есть только у админа и только по ask().
// Один источник: {name, origin} приходит с сервера, страница не выводит
// имя из origin второй раз. Ответ читается один раз и живёт в памяти
// страницы: реестр не меняется каждые полсекунды, и опрос ему не нужен --
// обновление это новая открытая форма. Смена источника (роль) стирает
// список в тот же рендер; в storage его нет (запрет #463).
import { useCallback, useEffect, useRef, useState } from "react";
import type { Session } from "./useSession";
import { refuseText } from "./usePuppetCard";

export interface ProjectRow { name: string; origin: string }

export interface ProjectsState {
  projects: ProjectRow[];
  error: string | null;
  /** Загрузить список, если он ещё не в памяти. Повторные ask -- бесплатно. */
  ask: () => void;
}

export function useProjects(session: Session, want: boolean): ProjectsState {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
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
      setProjects([]);
      setError(null);
    }
  }, [source, stale]);

  const ask = useCallback(() => {
    if (asked.current) return;
    asked.current = true;
    if (session.role !== "admin") return;   // форме не-admin не видна вовсе
    const at = ++seq.current;
    fetch("/api/admin/projects", { credentials: "same-origin" })
      .then(async (r) => {
        if (at !== seq.current) return;
        if (!r.ok) { setError(refuseText(r.status)); return; }
        const body = (await r.json()) as { projects?: ProjectRow[] };
        if (at !== seq.current) return;
        setProjects(Array.isArray(body.projects) ? body.projects : []);
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

  if (stale) return { projects: [], error: null, ask };
  return { projects, error, ask };
}
