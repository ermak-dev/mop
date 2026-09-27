import "@testing-library/jest-dom/vitest";

// Mantine в jsdom: matchMedia и ResizeObserver у jsdom нет.
window.matchMedia = window.matchMedia || ((query: string) => ({
  matches: false, media: query, onchange: null,
  addListener: () => {}, removeListener: () => {},
  addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
} as MediaQueryList));
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
  (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver
  || class { observe() {} unobserve() {} disconnect() {} };
