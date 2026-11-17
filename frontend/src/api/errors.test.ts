import { describe, expect, it } from "vitest";

import { ApiError, isApiError, kindForStatus, parseErrorBody, parseRetryAfter } from "./errors";

describe("parseErrorBody", () => {
  it("reads DRF field errors and non-field errors", () => {
    const error = parseErrorBody(400, {
      name: ["This field is required."],
      non_field_errors: ["Profile has no skills."],
    });
    expect(error.fieldErrors).toEqual({
      name: ["This field is required."],
      non_field_errors: ["Profile has no skills."],
    });
    expect(error.fieldMessage()).toBe("Profile has no skills.");
    expect(error.message).toBe("name: This field is required.");
  });

  it("uses detail as the message", () => {
    const error = parseErrorBody(404, { detail: "Not found." });
    expect(error).toMatchObject({ kind: "not_found", detail: "Not found.", message: "Not found." });
  });

  it("flattens nested serializer errors", () => {
    const error = parseErrorBody(400, {
      contacts: [{ email: ["Enter a valid email address."] }, {}],
      company: { website: ["Enter a valid URL."] },
    });
    expect(error.fieldErrors.contacts).toEqual(["email: Enter a valid email address."]);
    expect(error.fieldErrors.company).toEqual(["website: Enter a valid URL."]);
  });

  it("keeps scalar extras (409 application id, collector run id)", () => {
    expect(parseErrorBody(409, { detail: "Another collector run is in progress.", run: 4 }).extra).toEqual({ run: 4 });
    const invalid = parseErrorBody(400, { profile: ["Multiple profiles exist."], run: 9 });
    expect(invalid.extra).toEqual({ run: 9 });
    expect(invalid.fieldErrors).toEqual({ profile: ["Multiple profiles exist."] });
  });

  it("keeps a structured non-validation body without inventing field errors", () => {
    const body = {
      run: { id: 3, status: "PARTIAL", counts: { created: 1 } },
      outcomes: [{ index: 0, status: "created", job_id: 5 }],
      detail: "browser crashed",
    };
    const error = parseErrorBody(502, body);
    expect(error).toMatchObject({ kind: "server", detail: "browser crashed", fieldErrors: {} });
    expect(error.body).toEqual(body);
  });

  it("treats a top-level list as non-field errors on validation", () => {
    expect(parseErrorBody(400, ["Expected a JSON object."]).fieldErrors).toEqual({
      non_field_errors: ["Expected a JSON object."],
    });
  });

  it("uses short plain text and ignores HTML pages", () => {
    expect(parseErrorBody(400, "Bad request").detail).toBe("Bad request");
    expect(parseErrorBody(400, "<html>...</html>").detail).toBeNull();
    expect(parseErrorBody(500, "Traceback ...").detail).toBeNull();
  });

  it("falls back to a generic message per kind", () => {
    expect(parseErrorBody(503, undefined).message).toBe("The backend reported an error.");
    expect(parseErrorBody(401, null).message).toBe("The API token was rejected.");
    expect(new ApiError({ status: 0 }).message).toBe("Cannot reach the ApplyForge backend.");
    expect(parseErrorBody(418, null).message).toBe("Request failed (HTTP 418).");
  });
});

describe("helpers", () => {
  it("maps statuses to kinds", () => {
    expect([0, 400, 401, 403, 404, 409, 422, 429, 500, 502, 418].map(kindForStatus)).toEqual([
      "network",
      "validation",
      "unauthorized",
      "forbidden",
      "not_found",
      "conflict",
      "validation",
      "throttled",
      "server",
      "server",
      "unknown",
    ]);
  });

  it("parses Retry-After seconds and dates", () => {
    expect(parseRetryAfter("30")).toBe(30);
    expect(parseRetryAfter("1.2")).toBe(2);
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
    const inTenSeconds = new Date(Date.now() + 10_000).toUTCString();
    expect(parseRetryAfter(inTenSeconds)).toBeGreaterThanOrEqual(9);
  });

  it("recognises ApiError instances", () => {
    expect(isApiError(new ApiError({ status: 400 }))).toBe(true);
    expect(isApiError(new Error("x"))).toBe(false);
  });
});
