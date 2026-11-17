import { describe, expect, it } from "vitest";

import { ApiError } from "../api/errors";
import { describeGenerationError } from "../features/applications/generationErrors";
import { INTERNAL_ERROR_TEXT, shortError } from "../features/collector/runText";
import { isExceptionText, publicMessage, technicalDetail } from "./diagnostics";

const OLLAMA_503 =
  "Cannot reach Ollama at http://localhost:11434/v1 ([WinError 10061] No connection could be made because the target machine actively refused it). Is Ollama running with model 'qwen3:1.7b' pulled?";

describe("diagnostics cleanup", () => {
  it("removes URLs and OS error codes", () => {
    const cleaned = technicalDetail(OLLAMA_503)!;
    expect(cleaned).not.toContain("http://");
    expect(cleaned).not.toContain("11434");
    expect(cleaned).not.toContain("WinError");
    expect(cleaned).toContain("Cannot reach Ollama at [address]");
    expect(technicalDetail("LinkedIn page https://www.linkedin.com/search/results/content/?keywords=x failed")).toBe(
      "LinkedIn page [address] failed",
    );
    expect(technicalDetail("   ")).toBeNull();
    expect(technicalDetail("x".repeat(400))).toHaveLength(301);
  });

  it("recognises raw exception text and never shows it directly", () => {
    for (const text of ["OperationalError: database is locked", "RuntimeError: boom", "django.db.utils.IntegrityError: UNIQUE constraint", "Traceback (most recent call last): ..."]) {
      expect(isExceptionText(text)).toBe(true);
      expect(publicMessage(text)).toBeNull();
    }
    for (const text of ["Not logged in to LinkedIn. Run linkedin_login on the backend machine.", "1 post(s) failed to ingest.", "Job seeker's post (not a hiring post)."]) {
      expect(isExceptionText(text)).toBe(false);
      expect(publicMessage(text)).toBe(text);
    }
  });

  it("collector errors: own messages kept, exceptions replaced", () => {
    expect(shortError("LinkedIn stopped responding after 2 posts.")).toBe("LinkedIn stopped responding after 2 posts.");
    expect(shortError("OperationalError: database is locked")).toBe(INTERNAL_ERROR_TEXT);
    expect(shortError("Timed out loading https://www.linkedin.com/feed/")).toBe("Timed out loading [address]");
    expect(shortError("")).toBeNull();
  });
});

describe("generation outcome classification", () => {
  const fromBackend = (status: number, detail: string) => new ApiError({ status, detail, extra: { application: 5 } });

  it("treats the generation endpoint's own failures as definite (drafts and status kept)", () => {
    for (const status of [503, 504, 502, 422, 409]) {
      expect(describeGenerationError(fromBackend(status, "x"))).toMatchObject({ confirmedUnchanged: true, uncertain: false });
    }
    expect(describeGenerationError(fromBackend(504, "Ollama did not respond within 180 seconds.")).title).toBe("The local model took too long");
  });

  it("treats a 5xx without the backend's marker (proxy/gateway) as unknown", () => {
    for (const status of [504, 502, 503, 500]) {
      const problem = describeGenerationError(new ApiError({ status, detail: null }));
      expect(problem).toMatchObject({ uncertain: true, confirmedUnchanged: false, title: "No confirmed result from the backend" });
    }
    // A proxy's JSON body without the application id is not trusted either.
    expect(describeGenerationError(new ApiError({ status: 502, detail: "Bad gateway" })).uncertain).toBe(true);
  });

  it("treats a dropped connection as unknown, and input/throttle errors as definite", () => {
    expect(describeGenerationError(new ApiError({ status: 0 }))).toMatchObject({ uncertain: true, confirmedUnchanged: false });
    expect(describeGenerationError(new Error("x"))).toMatchObject({ uncertain: true, confirmedUnchanged: false });
    expect(describeGenerationError(new ApiError({ status: 400, fieldErrors: { resume: ["Attach a resume."] } }))).toMatchObject({
      uncertain: false,
      confirmedUnchanged: true,
      message: "Attach a resume.",
    });
    expect(describeGenerationError(new ApiError({ status: 429, retryAfterSeconds: 9 }))).toMatchObject({ uncertain: false });
  });

  it("never puts internal details in the directly shown message", () => {
    const problem = describeGenerationError(fromBackend(503, OLLAMA_503));
    expect(problem.message).toBeNull();
    expect(`${problem.title} ${problem.advice}`).not.toMatch(/localhost|11434|qwen|WinError/);
    expect(problem.technical).toBe(OLLAMA_503); // cleaned when rendered by <TechnicalDetails>
  });
});
