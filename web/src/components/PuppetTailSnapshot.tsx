import { useLayoutEffect, useRef, useState } from 'react';
import { Box, Group, Loader, Stack, Text } from '@mantine/core';
import { IconRefresh, IconZoomIn, IconZoomOut } from '@tabler/icons-react';
import type { PuppetTailState } from '../usePuppetCard';
import { IconButton } from './IconButton';
import { LABELS } from './labels';

export function PuppetTailSnapshot({ tail, visible, refresh }: {
  tail: PuppetTailState; visible: boolean; refresh: () => Promise<void>;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const text = useRef<HTMLPreElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0, available: 0, viewportHeight: 0 });
  const [zoom, setZoom] = useState<number | null>(null);
  const atEnd = useRef(true);
  const logicalTop = useRef(0);
  const scale = zoom ?? (size.width && size.available ? Math.min(1, size.available / size.width) : 1);

  useLayoutEffect(() => {
    if (!visible) return;
    const measure = () => {
      if (!viewport.current || !text.current) return;
      const next = { width: text.current.offsetWidth, height: text.current.offsetHeight,
        available: viewport.current.clientWidth, viewportHeight: viewport.current.clientHeight };
      setSize(prev => prev.width === next.width && prev.height === next.height
        && prev.available === next.available && prev.viewportHeight === next.viewportHeight ? prev : next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (viewport.current) observer.observe(viewport.current);
    if (text.current) observer.observe(text.current);
    return () => observer.disconnect();
  }, [visible, tail.lines, tail.loaded, tail.blocked]);

  useLayoutEffect(() => {
    if (!visible || !viewport.current) return;
    viewport.current.scrollTop = atEnd.current
      ? viewport.current.scrollHeight : logicalTop.current * scale;
  }, [visible, tail.lines, size.height, size.viewportHeight, scale]);

  const changeZoom = (delta: number) => setZoom(Math.max(0.1, Math.min(4, scale + delta)));
  const loading = !tail.loaded;
  return <div className="hybrid-snapshot" style={{ display: visible ? 'flex' : 'none' }}>
    <Group role="toolbar" aria-label="просмотр" justify="space-between" wrap="nowrap" gap={4}
      px="md" pb="xs" style={{ flexShrink: 0, backgroundColor: 'var(--mantine-color-gray-0)', borderBottom: '1px solid var(--mantine-color-default-border)' }}>
      <Group gap={4} wrap="nowrap">
        <IconButton label="Уменьшить" variant="subtle" size="lg" disabled={scale <= 0.1}
          onClick={() => changeZoom(-0.25)}><IconZoomOut size={18} /></IconButton>
        <Text size="sm" w={44} ta="center" aria-label="масштаб">{Math.round(scale * 100)}%</Text>
        <IconButton label="Увеличить" variant="subtle" size="lg" disabled={scale >= 4}
          onClick={() => changeZoom(0.25)}><IconZoomIn size={18} /></IconButton>
      </Group>
      <Group gap="xs" wrap="nowrap">
        {tail.mode === 'initialization' && <Text size="xs" c="dimmed">инициализация</Text>}
        {!loading && tail.truncated && <Text size="xs" c="dimmed">обрезано: показан конец</Text>}
        <IconButton label={LABELS.refresh} variant="subtle" size="lg" disabled={loading}
          onClick={() => { void refresh(); }}><IconRefresh size={18} /></IconButton>
      </Group>
    </Group>
    {tail.error && <Text role="alert" c="red" size="xs" px="md" pt="xs" style={{ flexShrink: 0 }}>
      {tail.error}{tail.lines.length ? '; показан последний снимок' : ''}
    </Text>}
    {tail.note && !tail.blocked && <Text size="xs" c="dimmed" px="md" pt="xs" style={{ flexShrink: 0 }}>{tail.note}</Text>}
    <Box p="md" style={{ flex: 1, minHeight: 0, display: 'flex', overflow: 'hidden' }}>
      <Box ref={viewport} data-testid="output-scroll" role="region" aria-label="вывод экрана" tabIndex={0}
        style={{ flex: 1, minWidth: '100%', minHeight: 0, overflow: 'auto', scrollbarGutter: 'stable', backgroundColor: 'var(--mantine-color-dark-8)', color: 'var(--mantine-color-gray-1)' }}
        onScroll={() => {
          const el = viewport.current!;
          atEnd.current = el.scrollHeight - el.clientHeight - el.scrollTop <= 2;
          logicalTop.current = el.scrollTop / scale;
        }}>
        {loading ? <Stack h="100%" align="center" justify="center" gap="sm" role="status" aria-label="загрузка экрана">
          <Loader size="sm" /><Text size="sm" c="dimmed">загрузка экрана</Text>
        </Stack> : tail.blocked ? null : <>
          <Box style={{ position: 'relative', width: Math.ceil(size.width * scale), height: Math.ceil(size.height * scale), minWidth: '100%', overflow: 'hidden' }}>
            <Box component="pre" ref={text} data-testid="tail-lines" m={0} style={{
              position: 'absolute', top: 0, left: 0, width: 'max-content', minWidth: '100%', whiteSpace: 'pre',
              fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 13, lineHeight: 1.45,
              transformOrigin: 'top left', transform: `scale(${scale})`,
            }}>{tail.lines.map((line, i) => <span key={i}>{line}{i < tail.lines.length - 1 ? '\n' : ''}</span>)}</Box>
          </Box>
          {!tail.lines.length && !tail.error && !tail.note && <Text size="sm" c="dimmed" ta="center" mt="xl">вывод пуст</Text>}
        </>}
      </Box>
    </Box>
  </div>;
}
