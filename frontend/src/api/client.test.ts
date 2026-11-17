import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { API, server } from "../test/server";
import { buildQueryString, createApiClient, resolveBaseUrl } from "./client";
import { ApiError } from "./errors";

const TOKEN = "test-token-abc123";

function client(overrides: Partial<Parameters<typeof createApiClient>[0]> = {}) {
  return createApiClient({ baseUrl: API, getToken: () => TOKEN, ...overrides });
}

describe("authentication header", () => {
  it("sends the bearer token on every request and no cookies", async () => {
    const seen: Request[] = [];
    server.use(
      http.all(`${API}/*`, ({ request }) => {
        seen.push(request.clone());
        return HttpResponse.json({ ok: true });
      }),
    );
    const api = client();
    await api.get("profiles/1/");
    await api.post("jobs/screen/", { jobs: [1] });
    await api.patch("jobs/1/", { status: "SKIPPED" });
    await api.delete("skills/2/");

    expect(seen).toHaveLength(4);
    for (const request of seen) {
      expect(request.headers.get("authorization")).toBe(`Bearer ${TOKEN}`);
      expect(request.headers.get("cookie")).toBeNull();
      expect(request.url).not.toContain(TOKEN);
    }
  });

  it("reads the current token for each request", async () => {
    let token = "first-token";
    const headers: (string | null)[] = [];
    server.use(
      http.get(`${API}/profiles/`, ({ request }) => {
        headers.push(request.headers.get("authorization"));
        return HttpResponse.json([]);
      }),
    );
    const api = client({ getToken: () => token });
    await api.list("profiles/");
    token = "second-token";
    await api.list("profiles/");
    expect(headers).toEqual(["Bearer first-token", "Bearer second-token"]);
  });

  it("makes no request without a token", async () => {
    const handler = vi.fn(() => HttpResponse.json([]));
    server.use(http.get(`${API}/profiles/`, handler));
    const onUnauthorized = vi.fn();
    const api = client({ getToken: () => null, onUnauthorized });

    await expect(api.list("profiles/")).rejects.toMatchObject({ status: 401, kind: "unauthorized" });
    expect(handler).not.toHaveBeenCalled();
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("sends JSON bodies with a JSON content type, and multipart uploads without one", async () => {
    const types: (string | null)[] = [];
    server.use(
      http.post(`${API}/*`, ({ request }) => {
        types.push(request.headers.get("content-type"));
        return HttpResponse.json({ id: 1 }, { status: 201 });
      }),
    );
    const api = client();
    await api.post("skills/", { name: "Python", profile: 1 });
    const form = new FormData();
    form.set("name", "CV");
    await api.upload("POST", "resumes/", form);

    expect(types[0]).toBe("application/json");
    expect(types[1]).toMatch(/^multipart\/form-data; boundary=/);
  });
});

describe("401 handling", () => {
  it("calls onUnauthorized and throws an unauthorized ApiError", async () => {
    server.use(
      http.get(`${API}/jobs/`, () =>
        HttpResponse.json({ detail: "Invalid token." }, { status: 401, headers: { "WWW-Authenticate": 'Bearer realm="api"' } }),
      ),
    );
    const onUnauthorized = vi.fn();
    const error = await client({ onUnauthorized }).list("jobs/").catch((e: unknown) => e);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 401, kind: "unauthorized", detail: "Invalid token." });
  });

  it("does not call onUnauthorized for other errors", async () => {
    server.use(http.get(`${API}/jobs/1/`, () => HttpResponse.json({ detail: "Not found." }, { status: 404 })));
    const onUnauthorized = vi.fn();
    await expect(client({ onUnauthorized }).get("jobs/1/")).rejects.toMatchObject({ kind: "not_found" });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("never includes the token in an error", async () => {
    server.use(http.get(`${API}/jobs/`, () => HttpResponse.json({ detail: "Invalid token." }, { status: 401 })));
    const error = (await client().list("jobs/").catch((e: unknown) => e)) as ApiError;
    const serialized = JSON.stringify({ ...error, message: error.message, stack: error.stack });
    expect(serialized).not.toContain(TOKEN);
  });
});

describe("pagination formats", () => {
  it("normalises a plain array (no page parameters sent)", async () => {
    let url = "";
    server.use(
      http.get(`${API}/skills/`, ({ request }) => {
        url = request.url;
        return HttpResponse.json([{ id: 1 }, { id: 2 }]);
      }),
    );
    const result = await client().list<{ id: number }>("skills/", { query: { profile: 3 } });

    expect(new URL(url).search).toBe("?profile=3");
    expect(result).toEqual({
      items: [{ id: 1 }, { id: 2 }],
      count: 2,
      paginated: false,
      hasNext: false,
      hasPrevious: false,
    });
  });

  it("normalises a DRF page and sends page and page_size", async () => {
    let params: URLSearchParams | null = null;
    server.use(
      http.get(`${API}/jobs/`, ({ request }) => {
        params = new URL(request.url).searchParams;
        return HttpResponse.json({
          count: 120,
          next: `${API}/jobs/?page=3&page_size=50`,
          previous: `${API}/jobs/?page=1&page_size=50`,
          results: [{ id: 9 }],
        });
      }),
    );
    const result = await client().list("jobs/", {
      query: { screening_outcome: "SHORTLISTED" },
      page: { page: 2, pageSize: 50 },
    });

    expect(params!.get("page")).toBe("2");
    expect(params!.get("page_size")).toBe("50");
    expect(params!.get("screening_outcome")).toBe("SHORTLISTED");
    expect(result).toEqual({ items: [{ id: 9 }], count: 120, paginated: true, hasNext: true, hasPrevious: true });
  });

  it("clamps page_size to the backend maximum", async () => {
    let size: string | null = null;
    server.use(
      http.get(`${API}/jobs/`, ({ request }) => {
        size = new URL(request.url).searchParams.get("page_size");
        return HttpResponse.json({ count: 0, next: null, previous: null, results: [] });
      }),
    );
    const result = await client().list("jobs/", { page: { pageSize: 1000 } });
    expect(size).toBe("200");
    expect(result).toMatchObject({ items: [], count: 0, paginated: true, hasNext: false });
  });

  it("rejects a list response of an unexpected shape", async () => {
    server.use(http.get(`${API}/jobs/`, () => HttpResponse.json({ unexpected: true })));
    await expect(client().list("jobs/")).rejects.toThrow(/Unexpected list response/);
  });
});

describe("errors and responses", () => {
  it("maps validation errors to field errors", async () => {
    server.use(
      http.post(`${API}/skills/`, () =>
        HttpResponse.json({ name: ["A skill with this name already exists for this profile."] }, { status: 400 }),
      ),
    );
    const error = (await client().post("skills/", { name: "Python" }).catch((e: unknown) => e)) as ApiError;
    expect(error.kind).toBe("validation");
    expect(error.fieldMessage("name")).toBe("A skill with this name already exists for this profile.");
  });

  it("keeps 409 extras such as the existing application id", async () => {
    server.use(
      http.post(`${API}/applications/`, () =>
        HttpResponse.json({ detail: "An application already exists.", application: 12 }, { status: 409 }),
      ),
    );
    const error = (await client().post("applications/", { job: 1 }).catch((e: unknown) => e)) as ApiError;
    expect(error).toMatchObject({ kind: "conflict", detail: "An application already exists.", extra: { application: 12 } });
  });

  it("reports 429 with the Retry-After delay", async () => {
    server.use(
      http.post(`${API}/collector/runs/`, () =>
        HttpResponse.json(
          { detail: "Request was throttled. Expected available in 42 seconds." },
          { status: 429, headers: { "Retry-After": "42" } },
        ),
      ),
    );
    const error = (await client().post("collector/runs/", {}).catch((e: unknown) => e)) as ApiError;
    expect(error).toMatchObject({ kind: "throttled", retryAfterSeconds: 42 });
  });

  it("turns an unreachable backend into a network error", async () => {
    server.use(http.get(`${API}/profiles/`, () => HttpResponse.error()));
    await expect(client().list("profiles/")).rejects.toMatchObject({ status: 0, kind: "network" });
  });

  it("does not show an HTML error page as the message", async () => {
    server.use(
      http.get(`${API}/jobs/`, () =>
        new HttpResponse("<!doctype html><title>Server Error</title>", {
          status: 500,
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );
    const error = (await client().list("jobs/").catch((e: unknown) => e)) as ApiError;
    expect(error.kind).toBe("server");
    expect(error.message).toBe("The backend reported an error.");
  });

  it("returns undefined for 204 No Content", async () => {
    server.use(http.delete(`${API}/skills/2/`, () => new HttpResponse(null, { status: 204 })));
    await expect(client().delete("skills/2/")).resolves.toBeUndefined();
  });

  it("passes aborts through unchanged", async () => {
    server.use(http.get(`${API}/jobs/`, () => new Promise(() => {})));
    const controller = new AbortController();
    const pending = client().list("jobs/", { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("URLs", () => {
  it("only accepts relative API paths", async () => {
    const api = client();
    for (const path of ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "../admin/", ""]) {
      await expect(api.get(path)).rejects.toThrow("API paths must be relative");
    }
  });

  it("resolves the base URL", () => {
    expect(resolveBaseUrl(undefined)).toBe("/api/v1");
    expect(resolveBaseUrl("/api/v1/")).toBe("/api/v1");
    expect(resolveBaseUrl("http://127.0.0.1:8000/api/v1")).toBe("http://127.0.0.1:8000/api/v1");
    expect(() => resolveBaseUrl("//evil.example")).toThrow();
    expect(() => resolveBaseUrl("ftp://x")).toThrow();
  });

  it("builds query strings, skipping empty values and joining lists", () => {
    expect(buildQueryString({ a: 1, b: "", c: null, d: undefined, e: true, status: ["SENT", "FOLLOW_UP"] })).toBe(
      "?a=1&e=true&status=SENT%2CFOLLOW_UP",
    );
    expect(buildQueryString({})).toBe("");
  });
});

describe("uploadWithProgress (XMLHttpRequest)", () => {
  const form = () => {
    const data = new FormData();
    data.set("profile", "1");
    data.set("file", new File(["%PDF-1.4"], "cv.pdf"), "cv.pdf");
    return data;
  };

  it("sends the token, reports progress and returns the body", async () => {
    let auth: string | null = null;
    server.use(
      http.post(`${API}/resumes/`, ({ request }) => {
        auth = request.headers.get("authorization");
        return HttpResponse.json({ id: 7 }, { status: 201 });
      }),
    );
    const progress: number[] = [];
    const result = await client().uploadWithProgress("POST", "resumes/", form(), { onProgress: (f) => progress.push(f) });
    expect(result).toEqual({ id: 7 });
    expect(auth).toBe(`Bearer ${TOKEN}`);
    expect(progress.at(-1)).toBe(1);
  });

  it("parses validation errors and calls onUnauthorized on 401", async () => {
    server.use(http.post(`${API}/resumes/`, () => HttpResponse.json({ file: ["The file is not a valid PDF."] }, { status: 400 })));
    const error = (await client().uploadWithProgress("POST", "resumes/", form()).catch((e: unknown) => e)) as ApiError;
    expect(error.fieldMessage("file")).toBe("The file is not a valid PDF.");

    server.use(http.post(`${API}/resumes/`, () => HttpResponse.json({ detail: "Invalid token." }, { status: 401 })));
    const onUnauthorized = vi.fn();
    await expect(client({ onUnauthorized }).uploadWithProgress("POST", "resumes/", form())).rejects.toMatchObject({
      kind: "unauthorized",
    });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("makes no request without a token, and rejects absolute paths", async () => {
    const handler = vi.fn(() => HttpResponse.json({}));
    server.use(http.post(`${API}/resumes/`, handler));
    await expect(client({ getToken: () => null }).uploadWithProgress("POST", "resumes/", form())).rejects.toMatchObject({
      status: 401,
    });
    expect(() => client().uploadWithProgress("POST", "https://evil.example/", form())).toThrow("relative");
    expect(handler).not.toHaveBeenCalled();
  });

  it("can be aborted", async () => {
    server.use(http.post(`${API}/resumes/`, () => new Promise(() => {})));
    const controller = new AbortController();
    const pending = client().uploadWithProgress("POST", "resumes/", form(), { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("maps a network failure", async () => {
    server.use(http.post(`${API}/resumes/`, () => HttpResponse.error()));
    await expect(client().uploadWithProgress("POST", "resumes/", form())).rejects.toMatchObject({ kind: "network" });
  });
});
