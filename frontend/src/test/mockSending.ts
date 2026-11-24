/**
 * Fake sending endpoints (backend/tracker/api/sending_views.py, tracker/sending.py).
 * NOTHING is sent: the handlers only record calls and update the mock state like the
 * backend would, so tests can assert what was requested (confirm: true, the preview
 * version, the hand-off record) and see the resulting per-channel `send_state`.
 */

import { http, HttpResponse, type HttpHandler } from "msw";

import type {
  ApplicationChannel,
  ApplicationDetail,
  ChannelSendState,
  SendChannel,
  SendPreview,
  SendRecord,
} from "../api/types";
import type { Call, MockState } from "./mockBackend";
import { API } from "./server";

const DONE: SendRecord["status"][] = ["SENT", "HANDED_OFF", "CONFIRMED", "VERIFIED"];
const POST_SEND = ["SENT", "FOLLOW_UP", "INTERVIEW", "OFFER", "REJECTED"];
const NAME: Record<string, string> = { EMAIL: "email", WHATSAPP: "WhatsApp" };
const PROVIDER = { mailbox: "mailbox_draft", eml: "eml_file", link: "whatsapp_link" } as const;

function blockFor(application: ApplicationDetail, channel: ApplicationChannel, records: SendRecord[], handoff = false): string {
  if (records.some((r) => r.channel === channel && DONE.includes(r.status))) return `Already sent by ${NAME[channel]}.`;
  if (records.some((r) => r.status === "COMPOSE_OPENED")) {
    return "You opened this application to send it yourself. Confirm that you sent it, or mark it as not sent, first.";
  }
  const sent = application.sent_at !== null || POST_SEND.includes(application.status);
  if (sent) return ["SENT", "FOLLOW_UP"].includes(application.status) ? "" : "This application was already sent.";
  // The hand-off (compose) also opens a generated draft: the click is the approval.
  const ready = handoff ? ["READY_TO_SEND", "SEND_FAILED", "GENERATED", "OPENED"] : ["READY_TO_SEND", "SEND_FAILED"];
  if (!ready.includes(application.status)) return handoff ? "Generate the drafts first." : "Approve the drafts first.";
  return "";
}

export function sendStateFor(records: SendRecord[]): Record<SendChannel, ChannelSendState> {
  const result = {} as Record<SendChannel, ChannelSendState>;
  for (const channel of ["EMAIL", "WHATSAPP"] as const) {
    const mine = records.filter((r) => r.channel === channel);
    const done = mine.find((r) => DONE.includes(r.status));
    const open = mine.find((r) => r.status === "COMPOSE_OPENED");
    const current = done ?? open ?? mine[0];
    result[channel] = {
      state: done ? "sent" : open ? "open" : mine[0]?.status === "FAILED" ? "failed" : "none",
      record: current?.id ?? null,
      status: current?.status ?? "",
      provider: current?.provider ?? "",
      recipient: current?.recipient ?? "",
      at: current ? (done ? current.finished_at : current.created_at) : null,
      open_url: open?.open_url ?? "",
      can_verify: open?.provider === "mailbox_draft",
      error: !done && !open && mine[0]?.status === "FAILED" ? mine[0].error : "",
    };
  }
  return result;
}

export function previewFor(state: MockState, application: ApplicationDetail, channel: ApplicationChannel, handoff = false): SendPreview {
  const email = channel === "EMAIL";
  const problems: string[] = [];
  const block = blockFor(application, channel, state.sendRecords[application.id] ?? [], handoff);
  if (block) problems.push(block);
  if (email && !state.sendingAccount) problems.push("Connect your email account (Account → Email sending) to send from ApplyForge.");
  const body = email ? (application.generated_body ?? "") : (application.generated_whatsapp_message ?? "");
  const preview: SendPreview = {
    channel,
    recipient: email ? "riya@fictional-labs.example" : "+44 7700 900123",
    recipient_source: "post",
    sender: state.sendingAccount ? `Maya Example <${state.sendingAccount.from_email}>` : "",
    subject: email ? (application.generated_subject ?? "") : "",
    body,
    attachment: { resume_id: 41, name: "Maya_Example_Resume.pdf", size: 24000, ok: true, problem: "" },
    whatsapp_link: email ? "" : `https://wa.me/447700900123?text=${encodeURIComponent(body)}`,
    problems,
    notes: email ? [] : ["WhatsApp links cannot attach files: attach your resume in WhatsApp before sending."],
    ready: problems.length === 0,
    version: `v-${application.id}-${application.status}-${body.length}`,
  };
  return preview;
}

export function sendingHandlers(
  state: MockState,
  record: (request: Request, extra?: Partial<Call>) => void,
  nextId: () => number,
): HttpHandler[] {
  const find = (id: unknown) => state.applications.find((a) => a.id === Number(id));
  let clock = 0;
  const stamp = () => `2026-03-01T11:${String(10 + (clock++ % 50)).padStart(2, "0")}:00Z`;
  const records = (application: ApplicationDetail) => state.sendRecords[application.id] ?? [];
  const sync = (application: ApplicationDetail, changes: Partial<ApplicationDetail> = {}) => {
    Object.assign(application, changes, { send_state: sendStateFor(records(application)), updated_at: stamp() });
  };
  const addRecord = (application: ApplicationDetail, preview: SendPreview, status: SendRecord["status"], extra: Partial<SendRecord> = {}) => {
    const now = stamp();
    const entry: SendRecord = {
      id: nextId(), channel: preview.channel, provider: preview.channel === "EMAIL" ? "smtp" : "whatsapp_link", status,
      recipient: preview.recipient, sender: preview.sender, subject: preview.subject, body: preview.body, resume: 41,
      attachment_name: preview.attachment.name, attachment_size: preview.attachment.size, message_id: "<draft@example.org>", error: "",
      open_url: "", details: {}, created_at: now, finished_at: status === "COMPOSE_OPENED" ? null : now, ...extra,
    };
    state.sendRecords[application.id] = [entry, ...records(application)];
    return entry;
  };
  const markSent = (application: ApplicationDetail, channel: ApplicationChannel) => {
    const sent = application.sent_at !== null || POST_SEND.includes(application.status);
    sync(application, sent ? {} : { status: "SENT", channel, sent_at: stamp() });
  };
  const openRecord = (application: ApplicationDetail, id: unknown) => {
    const entry = records(application).find((r) => r.id === Number(id));
    if (!entry) return { error: HttpResponse.json({ record: ["This hand-off does not belong to this application."] }, { status: 404 }) };
    if (DONE.includes(entry.status)) return { error: HttpResponse.json({ record: ["This message is already recorded as sent."] }, { status: 409 }) };
    if (entry.status !== "COMPOSE_OPENED") return { error: HttpResponse.json({ record: ["This message is no longer open."] }, { status: 409 }) };
    return { entry };
  };

  return [
    http.get(`${API}/sending/email-account/`, ({ request }) => {
      record(request);
      return HttpResponse.json({ account: state.sendingAccount, mode: state.sendingMode, drafts_mode: state.sendingMode === "smtp" ? "imap" : "local" });
    }),
    http.put(`${API}/sending/email-account/`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      if (!state.sendingAccount && !body.password) {
        return HttpResponse.json({ password: ["Enter the password (an app password for Gmail/Outlook)."] }, { status: 400 });
      }
      const rest = { ...body };
      delete rest.password; // write-only: never stored or returned
      state.sendingAccount = {
        from_name: "", from_email: "", smtp_host: "", smtp_port: 587, security: "starttls", username: "",
        imap_host: "", imap_port: 993, drafts_server: "", drafts_page: "",
        ...state.sendingAccount, ...(rest as object), has_password: true, verified_at: null, last_error: "",
        updated_at: `2026-03-01T10:0${Object.keys(body).length % 10}:00Z`,
      };
      const host = state.sendingAccount.smtp_host;
      state.sendingAccount.drafts_server = state.sendingAccount.imap_host || (host.startsWith("smtp.") ? `imap.${host.slice(5)}` : "");
      state.sendingAccount.drafts_page = host === "smtp.gmail.com" && state.sendingMode === "smtp" ? "https://mail.google.com/mail/?authuser=x#drafts" : "";
      return HttpResponse.json({ account: state.sendingAccount, mode: state.sendingMode, drafts_mode: state.sendingMode === "smtp" ? "imap" : "local" });
    }),
    http.post(`${API}/sending/email-account/test/`, ({ request }) => {
      record(request);
      if (!state.sendingAccount) return HttpResponse.json({ detail: "Save your email account first." }, { status: 400 });
      state.sendingAccount = { ...state.sendingAccount, verified_at: "2026-03-01T10:30:00Z" };
      return HttpResponse.json({
        ok: true, account: state.sendingAccount, mode: state.sendingMode, drafts_mode: state.sendingMode === "smtp" ? "imap" : "local",
        drafts: { ok: true, folders: { drafts: "Drafts", sent: "Sent" }, error: "" },
      });
    }),
    http.delete(`${API}/sending/email-account/`, ({ request }) => {
      record(request);
      state.sendingAccount = null;
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${API}/applications/:id/send-preview/`, ({ request, params }) => {
      record(request);
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const channel = (new URL(request.url).searchParams.get("channel") ?? application.channel) as ApplicationChannel;
      return HttpResponse.json(previewFor(state, application, channel));
    }),
    http.get(`${API}/applications/:id/send-records/`, ({ request, params }) => {
      record(request);
      return HttpResponse.json(state.sendRecords[Number(params.id)] ?? []);
    }),
    http.post(`${API}/applications/:id/send/`, async ({ request, params }) => {
      const body = (await request.json()) as { version?: string; confirm?: boolean };
      record(request, { body });
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const preview = previewFor(state, application, "EMAIL");
      if (body.confirm !== true) return HttpResponse.json({ confirm: ["Sending needs your explicit confirmation."] }, { status: 400 });
      if (!preview.ready) return HttpResponse.json({ non_field_errors: [preview.problems.join(" ")] }, { status: 400 });
      if (body.version !== preview.version) return HttpResponse.json({ version: ["The message changed since you reviewed it."] }, { status: 409 });
      const entry = addRecord(application, preview, "SENT");
      markSent(application, "EMAIL");
      return HttpResponse.json({ record: entry, application });
    }),
    http.post(`${API}/applications/:id/whatsapp-sent/`, async ({ request, params }) => {
      const body = (await request.json()) as { version?: string; confirm?: boolean };
      record(request, { body });
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const preview = previewFor(state, application, "WHATSAPP");
      if (body.confirm !== true || body.version !== preview.version) return HttpResponse.json({ confirm: ["Confirm first."] }, { status: 400 });
      const entry = addRecord(application, preview, "HANDED_OFF");
      markSent(application, "WHATSAPP");
      return HttpResponse.json({ record: entry, application });
    }),

    // --- Hand-off ---------------------------------------------------------------
    http.post(`${API}/applications/:id/compose/`, async ({ request, params }) => {
      const body = (await request.json()) as { channel: SendChannel; method: keyof typeof PROVIDER; version: string };
      record(request, { body });
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const allowed = body.channel === "EMAIL" ? ["mailbox", "eml"] : ["link"];
      if (!allowed.includes(body.method)) return HttpResponse.json({ method: ["Choose how to open this message."] }, { status: 400 });
      const preview = previewFor(state, application, body.channel, true);
      if (!preview.ready) return HttpResponse.json({ non_field_errors: [preview.problems.join(" ")] }, { status: 400 });
      if (body.version && body.version !== preview.version) return HttpResponse.json({ version: ["The message changed since you reviewed it."] }, { status: 409 });
      const local = state.sendingMode !== "smtp";
      const entry = addRecord(application, preview, "COMPOSE_OPENED", {
        provider: PROVIDER[body.method],
        open_url: body.method === "link" ? preview.whatsapp_link : body.method === "mailbox" ? (state.sendingAccount?.drafts_page ?? "") : "",
        details: body.method === "mailbox" ? { method: "mailbox", folder: "Drafts", mailbox: local ? "local" : "imap" } : { method: body.method },
      });
      const sent = application.sent_at !== null || POST_SEND.includes(application.status);
      sync(application, sent ? {} : { status: "COMPOSE_OPENED", last_error: null });
      const download_url = body.method === "eml" ? `applications/${application.id}/compose/draft/?record=${entry.id}` : "";
      return HttpResponse.json({ record: entry, application, download_url });
    }),
    http.post(`${API}/applications/:id/compose/confirm/`, async ({ request, params }) => {
      const body = (await request.json()) as { record: number; confirm?: boolean };
      record(request, { body });
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      if (body.confirm !== true) return HttpResponse.json({ confirm: ["Confirm that you sent the message."] }, { status: 400 });
      const { entry, error } = openRecord(application, body.record);
      if (error) return error;
      Object.assign(entry, { status: "CONFIRMED", finished_at: stamp() });
      markSent(application, entry.channel);
      return HttpResponse.json({ record: entry, application });
    }),
    http.post(`${API}/applications/:id/compose/verify/`, async ({ request, params }) => {
      const body = (await request.json()) as { record: number };
      record(request, { body });
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const { entry, error } = openRecord(application, body.record);
      if (error) return error;
      if (!state.sentFolderHasDraft) {
        return HttpResponse.json({ record: entry, application, verified: false, detail: `No email to ${entry.recipient} sent after 11:10 was found in your Sent folder yet.` });
      }
      Object.assign(entry, { status: "VERIFIED", finished_at: stamp(), details: { ...entry.details, evidence: { match: "recipient-and-time", subject: entry.subject, date: "" } } });
      markSent(application, entry.channel);
      return HttpResponse.json({ record: entry, application, verified: true, detail: "" });
    }),
    http.post(`${API}/applications/:id/compose/abandon/`, async ({ request, params }) => {
      const body = (await request.json()) as { record: number };
      record(request, { body });
      const application = find(params.id);
      if (!application) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const { entry, error } = openRecord(application, body.record);
      if (error) return error;
      Object.assign(entry, { status: "ABANDONED", finished_at: stamp() });
      sync(application, application.status === "COMPOSE_OPENED" ? { status: "READY_TO_SEND" } : {});
      return HttpResponse.json({ record: entry, application });
    }),
    http.get(`${API}/applications/:id/compose/draft/`, ({ request }) => {
      record(request);
      return new HttpResponse("X-Unsent: 1\r\n\r\nfictional draft", { headers: { "Content-Type": "message/rfc822" } });
    }),
  ];
}
