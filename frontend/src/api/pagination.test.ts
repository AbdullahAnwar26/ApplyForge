import { describe, expect, it } from "vitest";

import { normalizeList, pageCount, pageQuery } from "./pagination";

describe("normalizeList", () => {
  it("handles a plain array", () => {
    expect(normalizeList([1, 2, 3])).toEqual({ items: [1, 2, 3], count: 3, paginated: false, hasNext: false, hasPrevious: false });
    expect(normalizeList([])).toMatchObject({ items: [], count: 0, paginated: false });
  });

  it("handles a DRF page", () => {
    expect(normalizeList({ count: 51, next: "http://x/?page=2", previous: null, results: [{ id: 1 }] })).toEqual({
      items: [{ id: 1 }],
      count: 51,
      paginated: true,
      hasNext: true,
      hasPrevious: false,
    });
  });

  it("rejects other shapes", () => {
    for (const body of [null, undefined, "x", { results: [] }, { count: 1, results: "x", next: null, previous: null }]) {
      expect(() => normalizeList(body)).toThrow(TypeError);
    }
  });
});

describe("pageQuery", () => {
  it("is empty when no paging is requested (plain array response)", () => {
    expect(pageQuery(undefined)).toEqual({});
    expect(pageQuery({})).toEqual({});
  });

  it("sends page and a clamped page_size", () => {
    expect(pageQuery({ page: 2, pageSize: 25 })).toEqual({ page: 2, page_size: 25 });
    expect(pageQuery({ page: 0 })).toEqual({ page: 1 });
    expect(pageQuery({ pageSize: 500 })).toEqual({ page_size: 200 });
    expect(pageQuery({ pageSize: 0 })).toEqual({ page_size: 1 });
  });
});

describe("pageCount", () => {
  it("is at least one page", () => {
    expect(pageCount(0)).toBe(1);
    expect(pageCount(50)).toBe(1);
    expect(pageCount(51)).toBe(2);
    expect(pageCount(401, 200)).toBe(3);
  });
});
