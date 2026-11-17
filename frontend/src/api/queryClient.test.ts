import { describe, expect, it } from "vitest";

import { ApiError } from "./errors";
import { createQueryClient, shouldRetryQuery } from "./queryClient";

describe("query retries", () => {
  it("retries a read once, only for network and server errors", () => {
    expect(shouldRetryQuery(0, new ApiError({ status: 0 }))).toBe(true);
    expect(shouldRetryQuery(0, new ApiError({ status: 502 }))).toBe(true);
    expect(shouldRetryQuery(1, new ApiError({ status: 502 }))).toBe(false);
    for (const status of [400, 401, 404, 409, 429]) {
      expect(shouldRetryQuery(0, new ApiError({ status }))).toBe(false);
    }
    expect(shouldRetryQuery(0, new Error("x"))).toBe(false);
  });

  it("never retries writes", () => {
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false);
  });
});
