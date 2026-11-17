import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../api/queryClient";
import { API, server } from "../test/server";
import { ConnectPage } from "./ConnectPage";
import { TokenProvider, useApi, useToken } from "./TokenProvider";
import { TOKEN_STORAGE_KEY } from "./tokenStorage";

const GOOD = "good-token-123";

function acceptOnly(token: string) {
  return http.get(`${API}/profiles/`, ({ request }) =>
    request.headers.get("authorization") === `Bearer ${token}`
      ? HttpResponse.json({ count: 0, next: null, previous: null, results: [] })
      : HttpResponse.json({ detail: "Invalid token." }, { status: 401 }),
  );
}

/** Minimal stand-in for the app: the Connect gate plus one authenticated query. */
function Probe() {
  const api = useApi();
  const { disconnect, mode } = useToken();
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.list("jobs/") });
  return (
    <div>
      <p>connected ({mode})</p>
      <p>{jobs.isSuccess ? `jobs: ${jobs.data.count}` : "loading jobs"}</p>
      <button onClick={() => disconnect()}>Disconnect</button>
    </div>
  );
}

function Gate() {
  const { isConnected } = useToken();
  return isConnected ? <Probe /> : <ConnectPage baseUrl={API} />;
}

function renderApp() {
  const queryClient = createQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <TokenProvider baseUrl={API}>
        <Gate />
      </TokenProvider>
    </QueryClientProvider>,
  );
  return queryClient;
}

async function connectWith(token: string, memory = false) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("API token"), token);
  if (memory) await user.click(screen.getByLabelText(/In memory only/));
  await user.click(screen.getByRole("button", { name: "Connect" }));
  return user;
}

describe("Connect screen and token provider", () => {
  it("verifies the token, keeps it in sessionStorage and authenticates later requests", async () => {
    server.use(
      acceptOnly(GOOD),
      http.get(`${API}/jobs/`, ({ request }) =>
        request.headers.get("authorization") === `Bearer ${GOOD}`
          ? HttpResponse.json([{ id: 1 }, { id: 2 }])
          : HttpResponse.json({ detail: "Invalid token." }, { status: 401 }),
      ),
    );
    renderApp();
    await connectWith(GOOD);

    expect(await screen.findByText("connected (session)")).toBeInTheDocument();
    expect(await screen.findByText("jobs: 2")).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe(GOOD);
    expect(window.localStorage.length).toBe(0);
    expect(document.body.innerHTML).not.toContain(GOOD);
  });

  it("memory-only mode stores nothing", async () => {
    server.use(acceptOnly(GOOD), http.get(`${API}/jobs/`, () => HttpResponse.json([])));
    renderApp();
    await connectWith(GOOD, true);

    expect(await screen.findByText("connected (memory)")).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("rejects a wrong token without storing it or showing it", async () => {
    server.use(acceptOnly(GOOD));
    renderApp();
    await connectWith("wrong-token-999");

    expect(await screen.findByRole("alert")).toHaveTextContent("The backend rejected this token");
    expect(screen.getByRole("alert")).not.toHaveTextContent("wrong-token-999");
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();
  });

  it("reports an unreachable backend", async () => {
    server.use(http.get(`${API}/profiles/`, () => HttpResponse.error()));
    renderApp();
    await connectWith(GOOD);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the ApplyForge backend");
  });

  it("rejects an empty token without a request", async () => {
    renderApp(); // no handlers: any request would fail the test
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the API token");
  });

  it("clears the token and cached data when a request returns 401", async () => {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, "expired-token");
    server.use(http.get(`${API}/jobs/`, () => HttpResponse.json({ detail: "Invalid token." }, { status: 401 })));
    const queryClient = renderApp();

    expect(await screen.findByText(/no longer accepts the saved token/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByLabelText("API token")).toHaveValue("");
    await waitFor(() => expect(queryClient.getQueryCache().getAll()).toHaveLength(0));
  });

  it("disconnect clears the stored token and cached data", async () => {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, GOOD);
    server.use(http.get(`${API}/jobs/`, () => HttpResponse.json([{ id: 1 }])));
    const queryClient = renderApp();
    expect(await screen.findByText("jobs: 1")).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Disconnect" }));

    expect(await screen.findByText(/Disconnected. The token was removed/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
