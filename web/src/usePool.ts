// Снимок пула: первый -- запросом /api/pool, дальше -- событиями /events
// (SSE, кадр `snapshot`). Тот же протокол, что у прежней страницы: EventSource
// сам переподключается, но пока сервер лежит, страница обязана хоть что-то
// показывать -- на обрыве потока включается опрос раз в десять секунд, а с
// первым кадром потока опрос выключается.
import { useEffect, useState } from "react";
import type { Snapshot } from "./types";

export const POLL_MS = 10_000;

export interface Pool { snapshot: Snapshot | null }

export function usePool(): Pool {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);

  useEffect(() => {
    let polling: ReturnType<typeof setInterval> | null = null;
    let closed = false;
    const load = () => fetch("/api/pool").then((r) => r.json()).then((s: Snapshot) => { if (!closed) setSnapshot(s); }).catch(() => {});
    const poll = () => { if (!polling) polling = setInterval(load, POLL_MS); };
    const stopPolling = () => { if (polling) { clearInterval(polling); polling = null; } };

    load();
    const es = new EventSource("/events");
    es.addEventListener("snapshot", (e) => {
      if (closed) return;
      setSnapshot(JSON.parse((e as MessageEvent).data) as Snapshot);
      stopPolling();
    });
    es.onerror = () => poll();
    return () => { closed = true; es.close(); stopPolling(); };
  }, []);

  return { snapshot };
}
