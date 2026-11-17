import { describe, expect, it, vi } from "vitest";

import { TOKEN_STORAGE_KEY, clearStoredToken, normalizeToken, readStoredToken, storeToken } from "./tokenStorage";

describe("tokenStorage", () => {
  it("stores the token in sessionStorage only", () => {
    expect(storeToken("abc")).toBe(true);
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("abc");
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(readStoredToken()).toBe("abc");
  });

  it("clears it", () => {
    storeToken("abc");
    clearStoredToken();
    expect(readStoredToken()).toBeNull();
  });

  it("survives blocked storage", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(storeToken("abc")).toBe(false);
    expect(readStoredToken()).toBeNull();
    expect(() => clearStoredToken()).not.toThrow();
  });

  it("normalises pasted tokens", () => {
    expect(normalizeToken("  abc123  ")).toBe("abc123");
    expect(normalizeToken("")).toBeNull();
    expect(normalizeToken("two words")).toBeNull();
    expect(normalizeToken("line\nbreak")).toBeNull();
  });
});
