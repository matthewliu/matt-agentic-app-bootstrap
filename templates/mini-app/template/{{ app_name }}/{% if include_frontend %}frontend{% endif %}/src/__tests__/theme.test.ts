import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { STORAGE_KEY, getThemeChoice, nextThemeChoice, setThemeChoice } from "../theme";

// An in-memory Storage. Some Node versions replace jsdom's localStorage with
// their own, which is undefined unless Node gets --localstorage-file.
function memoryStorage(blocked = false): Storage {
  const data = new Map<string, string>();
  const guard = () => {
    if (blocked) throw new Error("storage blocked");
  };
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    key: (index) => [...data.keys()][index] ?? null,
    getItem: (key) => {
      guard();
      return data.get(key) ?? null;
    },
    setItem: (key, value) => {
      guard();
      data.set(key, String(value));
    },
    removeItem: (key) => {
      guard();
      data.delete(key);
    },
  };
}

function useStorage(storage: Storage) {
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true });
}

beforeEach(() => useStorage(memoryStorage()));

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
});

describe("theme choice", () => {
  it("defaults to system", () => {
    expect(getThemeChoice()).toBe("system");
  });

  it("applies and saves a manual choice", () => {
    setThemeChoice("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("dark");
    expect(getThemeChoice()).toBe("dark");
  });

  it("system removes the override and the saved value", () => {
    setThemeChoice("light");
    setThemeChoice("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("ignores an unknown saved value", () => {
    window.localStorage.setItem(STORAGE_KEY, "purple");
    expect(getThemeChoice()).toBe("system");
  });

  it("still applies the choice when storage is blocked", () => {
    useStorage(memoryStorage(true));
    setThemeChoice("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(getThemeChoice()).toBe("system");
  });

  it("cycles system, light, dark", () => {
    expect(nextThemeChoice("system")).toBe("light");
    expect(nextThemeChoice("light")).toBe("dark");
    expect(nextThemeChoice("dark")).toBe("system");
  });
});
