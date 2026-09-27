// Папеты по проектам (#298): секция на проект, колонки и корзины со старой страницы.
import { render, screen, within } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { PoolTable, projectNote } from "./PoolTable";
import type { Project } from "../types";

const PROJECTS: Project[] = [{
  name: "mop",
  counts: { puppets: 2, free: 1, busy: 1, sick: 0, silent: 0, down: 0 },
  puppets: [
    { name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "free (master)", kind: "free",
      owner: "", llm: "claude", origin: "git@h:g/mop.git", disk_kb: 2 * 1048576 },
    { name: "pu-mop-2", node: "gpu", alloc_status: "running", state: "busy: #12", kind: "busy",
      owner: "ermak", llm: "glm", origin: "git@h:g/mop.git", disk_kb: null },
  ],
}];

test("projectNote names the buckets in order", () => {
  expect(projectNote(PROJECTS[0].counts)).toBe("1 свободен, 1 занят");
  expect(projectNote({ puppets: 0, free: 0, busy: 0, sick: 0, silent: 0, down: 0 })).toBe("");
});

test("a section per project with the old page's columns and buckets", () => {
  render(<MantineProvider><PoolTable projects={PROJECTS} /></MantineProvider>);
  expect(screen.getByRole("heading", { name: /mop/ })).toHaveTextContent("1 свободен, 1 занят");
  for (const h of ["папет", "узел", "аллокация", "состояние", "пользователь", "модель", "место", "репозиторий"]) {
    expect(screen.getByRole("columnheader", { name: h })).toBeInTheDocument();
  }
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveAttribute("data-kind", "free");
  expect(within(rows[0]).getByText("2 GB")).toBeInTheDocument();
  expect(within(rows[0]).getByText("-")).toBeInTheDocument();
  expect(rows[1]).toHaveAttribute("data-kind", "busy");
  expect(within(rows[1]).getByText("ermak")).toBeInTheDocument();
  expect(within(rows[1]).getByText("busy: #12")).toBeInTheDocument();
});

test("no projects: a plain note", () => {
  render(<MantineProvider><PoolTable projects={[]} /></MantineProvider>);
  expect(screen.getByText("папетов нет")).toBeInTheDocument();
});
