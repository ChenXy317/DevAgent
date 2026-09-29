import { useSyncExternalStore } from "react";

export type ThemeName = "dark" | "light";
export type AccentName = "cyan" | "green" | "purple" | "amber";

type ThemeState = {
  theme: ThemeName;
  accent: AccentName;
};

const STORAGE_KEY = "coding-agent-theme";

const listeners = new Set<() => void>();

function read(): ThemeState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { theme: "dark", accent: "cyan" };
    }
    const parsed = JSON.parse(raw) as Partial<ThemeState>;
    const theme = parsed.theme === "light" ? "light" : "dark";
    const accent = ["cyan", "green", "purple", "amber"].includes(parsed.accent ?? "")
      ? (parsed.accent as AccentName)
      : "cyan";
    return { theme, accent };
  } catch {
    return { theme: "dark", accent: "cyan" };
  }
}

let state = read();

function emit() {
  listeners.forEach((listener) => listener());
}

function save(next: ThemeState) {
  state = next;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  document.documentElement.dataset.theme = next.theme;
  document.documentElement.dataset.accent = next.accent;
  emit();
}

export function applyStoredTheme() {
  document.documentElement.dataset.theme = state.theme;
  document.documentElement.dataset.accent = state.accent;
}

export function setTheme(theme: ThemeName) {
  save({ ...state, theme });
}

export function setAccent(accent: AccentName) {
  save({ ...state, accent });
}

export function useTheme() {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => state,
  );
}
