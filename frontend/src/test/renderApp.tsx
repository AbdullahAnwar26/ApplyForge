import { render } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";

import { App } from "../App";
import { createQueryClient } from "../api/queryClient";
import type { Profile } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { authGate, authHandlers, mockSession, resetMockSession, signedOutResponse } from "./mockAuth";
import { API, server } from "./server";

/** A fictional profile. */
export function makeProfile(id: number, name: string, overrides: Partial<Profile> = {}): Profile {
  return {
    id,
    name,
    professional_summary: null,
    target_roles: "Backend Developer",
    experience_info: null,
    preferred_locations: null,
    preferred_work_modes: null,
    experience_years: 2,
    max_acceptable_experience_years: 5,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** Serve GET profiles/ (plain array, as the backend does without paging). */
export function profilesHandler(profiles: Profile[]) {
  return http.get(`${API}/profiles/`, () => (mockSession.user ? HttpResponse.json(profiles) : signedOutResponse()));
}

/**
 * Render the whole app at `path`, already signed in (as TEST_USER) unless `signedIn` is false.
 * Adds the fake account endpoints and the auth gate in front of handlers registered earlier.
 */
export function renderApp({ path = "/", signedIn = true }: { path?: string; signedIn?: boolean } = {}) {
  resetMockSession(signedIn);
  server.use(authGate(), ...authHandlers());
  const queryClient = createQueryClient();
  const utils = render(
    <AppProviders queryClient={queryClient} baseUrl={API}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </AppProviders>,
  );
  return { ...utils, queryClient };
}
