import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Box, Code, Group, Loader, Modal, Paper, SimpleGrid, Stack, Tabs, Text } from "@mantine/core";
import { IconRefresh, IconTerminal2, IconFileAlert, IconZoomIn, IconArrowsHorizontal } from "@tabler/icons-react";
import type { Snapshot } from "../types";
import type { Session } from "../useSession";
import { usePuppetCard, usePuppetTail } from "../usePuppetCard";
import { IconButton } from "./IconButton";
import { gb, verdictColor, verdictWord } from "./format";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return <Box style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <Text size="xs" c="dimmed">{label}</Text>
    <Text component="div" size="sm">{children}</Text>
  </Box>;
}

/** Терминальные колонки не переносятся: масштаб измеряется по реальной
 * ширине шрифта, а не по числу символов (кириллица и широкие глифы). */
function TailLines({ lines, fitWidth }: { lines: string[]; fitWidth: boolean }) {
  const viewport = useRef<HTMLDivElement>(null);
  const text = useRef<HTMLPreElement>(null);
  const [size, setSize] = useState({ scale: 1, height: undefined as number | undefined });
  useLayoutEffect(() => {
    const fit = () => {
      if (!viewport.current || !text.current) return;
      const width = viewport.current.clientWidth;
      const natural = text.current.scrollWidth;
      const scale = fitWidth && width && natural ? Math.min(1, width / natural) : 1;
      setSize({ scale, height: text.current.scrollHeight * scale });
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(viewport.current!);
    observer.observe(text.current!);
    return () => observer.disconnect();
  }, [lines, fitWidth]);
  return <Box ref={viewport} style={{
    overflowX: fitWidth ? "hidden" : "auto", overflowY: "hidden",
    height: fitWidth ? size.height : "auto",
  }}>
    <Box component="pre" ref={text} data-testid="tail-lines" m={0} style={{
      width: "max-content", whiteSpace: "pre", fontFamily: "var(--mantine-font-family-monospace)",
      fontSize: 13, lineHeight: 1.45, transformOrigin: "top left", transform: `scale(${size.scale})`,
    }}>{lines.map((line, i) => <span key={i}>{line}{i < lines.length - 1 ? "\n" : ""}</span>)}</Box>
  </Box>;
}

function PuppetScreen({ session, target }: { session: Session; target: string }) {
  const [which, setWhich] = useState<"tail" | "stderr">("tail");
  const [requested, setRequested] = useState(false);
  const [fitWidth, setFitWidth] = useState(true);
  const tail = usePuppetTail(session, target, which);
  // После выбора вкладки эффект получает load именно нового источника.
  useEffect(() => { if (requested) void tail.load(); }, [requested, tail.load]);
  const select = (value: string | null) => {
    if (value !== "tail" && value !== "stderr") return;
    if (value === which && requested) void tail.load();
    setWhich(value);
    setRequested(true);
  };
  return <Stack gap="xs">
    <Group justify="space-between" wrap="nowrap">
      <Tabs value={which} onChange={select}>
        <Tabs.List aria-label="вывод папета">
          <Tabs.Tab value="tail" leftSection={<IconTerminal2 size={16} />}
            onClick={() => { if (!requested) setRequested(true); }}>экран</Tabs.Tab>
          <Tabs.Tab value="stderr" leftSection={<IconFileAlert size={16} />}>stderr</Tabs.Tab>
        </Tabs.List>
      </Tabs>
      <Group gap={4} wrap="nowrap">
        <IconButton label={fitWidth ? "масштаб 1:1" : "подогнать по ширине"}
          variant="subtle" size="lg" onClick={() => setFitWidth(!fitWidth)}>
          {fitWidth ? <IconZoomIn size={18} /> : <IconArrowsHorizontal size={18} />}
        </IconButton>
        <IconButton label="обновить" variant="subtle" size="lg" disabled={tail.loading}
          onClick={() => { if (requested) void tail.load(); else setRequested(true); }}>
          <IconRefresh size={18} />
        </IconButton>
      </Group>
    </Group>
    <Paper withBorder radius="md" p="md" mih={220} style={{ overflow: "hidden" }}>
      {tail.loading ? <Stack align="center" justify="center" mih={188} gap="sm"
        role="status" aria-label={which === "tail" ? "загрузка экрана" : "загрузка stderr"}>
        <Loader size="sm" /><Text size="sm" c="dimmed">{which === "tail" ? "загрузка экрана" : "загрузка stderr"}</Text>
      </Stack> : tail.error ? <Text c="red" role="alert">{tail.error}</Text>
        : tail.lines.length ? <TailLines lines={tail.lines} fitWidth={fitWidth} />
        : <Text size="sm" c="dimmed" ta="center" mt="xl">
          {tail.loaded ? "вывод пуст" : "выберите экран или stderr для загрузки"}
        </Text>}
    </Paper>
    {!tail.loading && tail.truncated && <Text size="xs" c="dimmed">обрезано: показан конец</Text>}
  </Stack>;
}

export function PuppetCard({ snapshot, session, target, onClose }: {
  snapshot: Snapshot;
  session: Session;
  target: string | null;
  onClose: () => void;
}) {
  const { card: row, error } = usePuppetCard(session, target);
  const at = snapshot.at ? new Date(snapshot.at * 1000).toLocaleTimeString() : "-";
  if (!target || session.role !== "admin") return null;
  return (
    <Modal opened onClose={onClose} size="min(1440px, calc(100vw - 32px))" yOffset={16}
      title={<Text fw={600}>папет {target}</Text>} data-testid="puppet-card"
      padding="lg" radius="md" closeButtonProps={{ "aria-label": "закрыть" }}>
      {error ? <Text c="red" role="alert">{error}</Text> : !row ? (
        <Group gap="sm" mih={220} justify="center" role="status" aria-label="загрузка карточки">
          <Loader size="sm" /><Text c="dimmed">карточка грузится</Text>
        </Group>
      ) : (
        <Stack gap="md">
          <Group justify="space-between" gap="xs">
            <Group gap="sm">
              <Text fw={600} c={verdictColor(row.kind, row.bucket)}>{verdictWord(row.kind)}</Text>
              {row.detail && <Text size="sm" c="dimmed">{row.detail}</Text>}
            </Group>
            <Text size="xs" c="dimmed">наблюдение в {at}</Text>
          </Group>
          <SimpleGrid cols={{ base: 2, sm: 3, md: 6 }} spacing="sm" verticalSpacing="xs">
            <Fact label="узел">{row.node || "-"}</Fact>
            <Fact label="ветка">{row.branch ? <Code>{row.branch}</Code> : "-"}</Fact>
            <Fact label="владелец">{row.owner || "-"}</Fact>
            <Fact label="модель">{row.llm || "-"}</Fact>
            <Fact label="место клона">{gb(row.disk_kb)}</Fact>
            <Fact label="state-строка"><Code>{row.state}</Code></Fact>
          </SimpleGrid>
          <Fact label="репозиторий">{row.origin ? <Code>{row.origin}</Code> : "-"}</Fact>
          <PuppetScreen key={`${session.login}\0${target}`} session={session} target={target} />
        </Stack>
      )}
    </Modal>
  );
}
