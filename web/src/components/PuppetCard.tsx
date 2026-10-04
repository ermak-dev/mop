import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Box, Group, Loader, Modal, Stack, Text } from "@mantine/core";
import { IconRefresh, IconZoomIn, IconZoomOut, IconArrowsHorizontal } from "@tabler/icons-react";
import type { Session } from "../useSession";
import { usePuppetTail } from "../usePuppetCard";
import { IconButton } from "./IconButton";

export type PuppetSource = "tail" | "stderr";

function PuppetOutput({ session, target, which }: {
  session: Session; target: string; which: PuppetSource;
}) {
  const tail = usePuppetTail(session, target, which);
  const viewport = useRef<HTMLDivElement>(null);
  const text = useRef<HTMLPreElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0, available: 0, viewportHeight: 0 });
  const [zoom, setZoom] = useState<number | null>(null);
  const atEnd = useRef(true);
  const logicalTop = useRef(0);
  const scale = zoom ?? (size.width && size.available ? Math.min(1, size.available / size.width) : 1);

  useEffect(() => { void tail.load(); }, [tail.load]);

  useLayoutEffect(() => {
    const measure = () => {
      if (!viewport.current || !text.current) return;
      const next = { width: text.current.offsetWidth, height: text.current.offsetHeight,
        available: viewport.current.clientWidth, viewportHeight: viewport.current.clientHeight };
      setSize((prev) => prev.width === next.width && prev.height === next.height
        && prev.available === next.available && prev.viewportHeight === next.viewportHeight ? prev : next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (viewport.current) observer.observe(viewport.current);
    if (text.current) observer.observe(text.current);
    return () => observer.disconnect();
  }, [tail.lines, tail.loading]);

  // Масштаб меняет геометрию, но не место чтения: конец остаётся концом,
  // а прокрученный вверх текст сохраняет логическую вертикальную позицию.
  useLayoutEffect(() => {
    if (!viewport.current) return;
    viewport.current.scrollTop = atEnd.current
      ? viewport.current.scrollHeight : logicalTop.current * scale;
  }, [tail.lines, size.height, size.viewportHeight, scale, tail.loading]);

  const changeZoom = (delta: number) => setZoom(Math.max(0.1, Math.min(4, scale + delta)));
  const loading = tail.loading || !tail.loaded;
  return <>
    <Group role="toolbar" aria-label="просмотр" justify="space-between" wrap="nowrap" gap={4}
      px="md" pb="xs" style={{ flexShrink: 0, borderBottom: "1px solid var(--mantine-color-default-border)" }}>
      <Group gap={4} wrap="nowrap">
        <IconButton label="уменьшить" variant="subtle" size="lg" disabled={scale <= 0.1}
          onClick={() => changeZoom(-0.25)}><IconZoomOut size={18} /></IconButton>
        <Text size="sm" w={44} ta="center" aria-label="масштаб">{Math.round(scale * 100)}%</Text>
        <IconButton label="увеличить" variant="subtle" size="lg" disabled={scale >= 4}
          onClick={() => changeZoom(0.25)}><IconZoomIn size={18} /></IconButton>
        <IconButton label="по ширине" variant={zoom === null ? "light" : "subtle"} size="lg"
          aria-pressed={zoom === null} onClick={() => setZoom(null)}><IconArrowsHorizontal size={18} /></IconButton>
      </Group>
      <Group gap="xs" wrap="nowrap">
        {!loading && tail.truncated && <Text size="xs" c="dimmed">обрезано: показан конец</Text>}
        <IconButton label="обновить" variant="subtle" size="lg" disabled={loading}
          onClick={() => { atEnd.current = true; void tail.load(); }}><IconRefresh size={18} /></IconButton>
      </Group>
    </Group>
    <Box p="md" style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
      <Box ref={viewport} data-testid="output-scroll" role="region"
        aria-label={which === "tail" ? "вывод экрана" : "вывод лога"} tabIndex={0}
        style={{ flex: 1, minWidth: 0, minHeight: 0, overflow: "auto", scrollbarGutter: "stable" }}
        onScroll={() => {
          const el = viewport.current!;
          atEnd.current = el.scrollHeight - el.clientHeight - el.scrollTop <= 2;
          logicalTop.current = el.scrollTop / scale;
        }}>
        {loading ? <Stack h="100%" align="center" justify="center" gap="sm"
          role="status" aria-label={which === "tail" ? "загрузка экрана" : "загрузка лога"}>
          <Loader size="sm" /><Text size="sm" c="dimmed">{which === "tail" ? "загрузка экрана" : "загрузка лога"}</Text>
        </Stack> : tail.error ? <Text c="red" role="alert">{tail.error}</Text>
          : tail.lines.length ? (
            // Рамка занимает только масштабированный размер; исходный pre
            // вне потока и не создаёт пустую область прокрутки за выводом.
            <Box style={{ position: "relative", width: Math.ceil(size.width * scale),
              height: Math.ceil(size.height * scale), minWidth: "100%", overflow: "hidden" }}>
              <Box component="pre" ref={text} data-testid="tail-lines" m={0} style={{
                position: "absolute", top: 0, left: 0, width: "max-content", whiteSpace: "pre",
                fontFamily: "var(--mantine-font-family-monospace)", fontSize: 13, lineHeight: 1.45,
                transformOrigin: "top left", transform: `scale(${scale})`,
              }}>{tail.lines.map((line, i) => <span key={i}>{line}{i < tail.lines.length - 1 ? "\n" : ""}</span>)}</Box>
            </Box>
          ) : <Text size="sm" c="dimmed" ta="center" mt="xl">вывод пуст</Text>}
      </Box>
    </Box>
  </>;
}

export function PuppetCard({ session, target, which = "tail", onClose }: {
  session: Session;
  target: string | null;
  which?: PuppetSource;
  onClose: () => void;
}) {
  if (!target || session.role !== "admin") return null;
  return <Modal opened onClose={onClose} size="min(1440px, calc(100vw - 32px))" yOffset={16}
    title={<Text fw={600}>{which === "tail" ? "экран" : "лог"} {target}</Text>}
    data-testid="puppet-card" padding="md" radius="md" closeButtonProps={{ "aria-label": "закрыть" }}
    styles={{
      content: { height: "calc(100dvh - 32px)", maxHeight: "calc(100dvh - 32px)",
        display: "flex", flexDirection: "column", overflow: "hidden" },
      header: { flexShrink: 0 },
      body: { flex: 1, minHeight: 0, padding: 0, display: "flex", flexDirection: "column", overflow: "hidden" },
    }}>
    <PuppetOutput key={`${session.login}\0${target}\0${which}`} session={session} target={target} which={which} />
  </Modal>;
}
