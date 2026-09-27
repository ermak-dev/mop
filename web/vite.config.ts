/// <reference types="vitest/config" />
// Сборка дашборда (docs/WEB.md, #297). Корень -- src/: там лежит входной
// index.html приложения, а web/index.html -- прежняя страница, которую
// снимает #301. Выход -- web/dist, он коммитится: на серверах node нет, и
// `mop dev web build --check` в CI сверяет dist с исходниками.
// base "./" -- ассеты относительными путями: страница отдаётся с `/`, и
// `./assets/x.js` превращается в `/assets/x.js`, что сервис и умеет.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "src",
  base: "./",
  publicDir: false,
  plugins: [react()],
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./setupTests.ts"],
    css: false,
  },
});
