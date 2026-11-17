import { render } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";

import { App } from "../App";
import { createQueryClient } from "../api/queryClient";
import type { Profile } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { TOKEN_STORAGE_KEY } from "../auth/tokenStorage";
import { API } from "./server";

export const TEST_TOKEN = "test-token-stage1";

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
  return http.get(`${API}/profiles/`, ({ request }) =>
    request.headers.get("authorization") === `Bearer ${TEST_TOKEN}`
      ? HttpResponse.json(profiles)
      : HttpResponse.json({ detail: "Invalid token." }, { status: 401 }),
  );
}

/** Render the whole app at `path`, already connected unless `connected` is false. */
export function renderApp({ path = "/", connected = true }: { path?: string; connected?: boolean } = {}) {
  if (connected) window.sessionStorage.setItem(TOKEN_STORAGE_KEY, TEST_TOKEN);
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
