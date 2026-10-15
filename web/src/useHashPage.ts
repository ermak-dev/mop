// Клиентская навигация (#422/#664): страницы дашборда живут на хеше
// адреса. web.py отдаёт index.html только на "/" (любой другой путь --
// 404), потому маршрут живёт в хеше: обновление страницы на #journal и
// прямая ссылка работают без сервера, back/forward -- родные.
import { useCallback, useEffect, useState } from "react";

export type Page = "main" | "nodes" | "tokens" | "journal";
const PAGES: Record<string, Page> = { "": "main", "#nodes": "nodes", "#tokens": "tokens", "#journal": "journal" };
const HASH: Record<Page, string> = { main: "", nodes: "#nodes", tokens: "#tokens", journal: "#journal" };

function fromHash(): Page {
  return PAGES[window.location.hash] ?? "main";   // чужой хеш -- главная
}

/** Страница по хешу; go -- переход без перезагрузки (pushState через
 *  присвоение hash: браузер сам кладёт запись в историю). */
export function useHashPage(): { page: Page; go: (p: Page) => void } {
  const [page, setPage] = useState<Page>(fromHash);
  useEffect(() => {
    const onHash = () => setPage(fromHash());      // и back/forward, и go
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((p: Page) => { window.location.hash = HASH[p]; }, []);
  return { page, go };
}
