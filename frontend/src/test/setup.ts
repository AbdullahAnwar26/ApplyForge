import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { clearMockSession } from "./mockAuth";
import { server } from "./server";

/**
 * Tests never reach a real backend, LinkedIn or any other network: a request with
 * no matching fake handler is refused (network error) AND fails the test that made
 * it, so a missing handler cannot pass silently as a generic failure.
 */
const unhandled: string[] = [];

// The first render of each test file (cold module load, parallel workers) can take
// over a second on a busy machine; waiting longer avoids flaky findBy timeouts.
configure({ asyncUtilTimeout: 5000 });

beforeAll(() =>
  server.listen({
    onUnhandledFrame: ({ frame, defaults }) => {
      const request = (frame as { data?: { request?: Request } }).data?.request;
      unhandled.push(request ? `${request.method} ${request.url}` : `${frame.protocol} frame`);
      defaults.error();
    },
  }),
);

afterEach(() => {
  server.resetHandlers();
  cleanup();
  window.sessionStorage.clear();
  window.localStorage.clear();
  clearMockSession();
  if (unhandled.length) {
    const list = unhandled.splice(0).join("\n  ");
    throw new Error(`Request(s) without a fake API handler:\n  ${list}`);
  }
});

afterAll(() => server.close());
