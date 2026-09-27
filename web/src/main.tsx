import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "@mantine/charts/styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import App from "./App";
import { DEFAULT_SCHEME } from "./components/ThemeSwitch";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider defaultColorScheme={DEFAULT_SCHEME}>
      <Notifications position="top-right" />
      <App />
    </MantineProvider>
  </StrictMode>,
);
