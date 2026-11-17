import { setupServer } from "msw/node";

/** Mock API for tests. Handlers are added per test with server.use(...). */
export const server = setupServer();

/** jsdom has no real origin for relative URLs; MSW matches requests against this one. */
export const API = "http://localhost:3000/api/v1";
