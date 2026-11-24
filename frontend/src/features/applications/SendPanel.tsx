/**
 * Sending one application. Nothing is ever sent without the user's own action:
 *
 *   1. Approve the drafts (status "Approved").
 *   2. "Send email…" / "Send WhatsApp…" shows the exact preview from the backend (recipient
 *      and whether it was found in the post, sender, subject, message, resume) and offers:
 *      - Email: save it as a draft in your own mailbox (resume attached) and open your
 *        Drafts; or download a draft file for a desktop email app; or (secondary) send it
 *        directly from ApplyForge after a checkbox confirmation.
 *      - WhatsApp: open WhatsApp with the message (attach the downloaded resume there).
 *   3. Opening is not sending: the application shows "Opened to send" until ApplyForge
 *      finds the email in your Sent folder ("Check my Sent folder") or you confirm
 *      ("I sent it"). "I didn't send it" closes it so it can be opened again.
 *
 * Each channel can be sent once; every attempt is listed under "Sending history".
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { applicationKeys, updateApplication } from "../../api/applications";
import { isApiError } from "../../api/errors";
import { jobKeys } from "../../api/jobs";
import {
  abandonHandoff,
  confirmHandoff,
  deliversForReal,
  draftFileUrl,
  getSendingAccount,
  getSendPreview,
  listSendRecords,
  openCompose,
  sendEmail,
  sendingKeys,
  verifyHandoff,
} from "../../api/sending";
import type {
  ApplicationDetail,
  ApplicationSummary,
  ChannelSendState,
  HandoffMethod,
  SendChannel,
  SendPreview,
  SendRecord,
  SendResult,
} from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { ErrorState, errorMessage, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { downloadFile, reserveWindow, type ReservedWindow } from "../../lib/external";
import { formatBytes, formatDateTime } from "../../lib/format";

const APPROVABLE = ["GENERATED", "OPENED"];
const CHANNEL_NAME: Record<SendChannel, string> = { EMAIL: "Email", WHATSAPP: "WhatsApp" };
const NO_STATE: ChannelSendState = {
  state: "none", record: null, status: "", provider: "", recipient: "", at: null, open_url: "", can_verify: false, error: "",
};

const SENT_HOW: Record<string, string> = {
  SENT: "sent by ApplyForge",
  HANDED_OFF: "confirmed by you",
  CONFIRMED: "confirmed by you",
  VERIFIED: "found in your Sent folder",
  MARKED: "marked as sent",
};

export const RECORD_LABEL: Record<SendRecord["status"], string> = {
  SENDING: "Sending",
  SENT: "Sent",
  FAILED: "Failed",
  HANDED_OFF: "Sent (confirmed by you)",
  COMPOSE_OPENED: "Opened for you to send",
  CONFIRMED: "Sent (confirmed by you)",
  VERIFIED: "Sent (found in your Sent folder)",
  ABANDONED: "Not sent",
};

function recordTone(status: SendRecord["status"]) {
  if (["SENT", "HANDED_OFF", "CONFIRMED", "VERIFIED"].includes(status)) return "success" as const;
  if (status === "FAILED") return "danger" as const;
  if (status === "COMPOSE_OPENED") return "warning" as const;
  return "neutral" as const;
}

function useRefresh(applicationId: number) {
  const queryClient = useQueryClient();
  return (result?: SendResult) => {
    if (result?.application) queryClient.setQueryData(applicationKeys.detail(applicationId), result.application);
    void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
    void queryClient.invalidateQueries({ queryKey: jobKeys.all });
    void queryClient.invalidateQueries({ queryKey: ["sending"] });
  };
}

function failureText(error: unknown) {
  if (isApiError(error) && error.status === 502) {
    const body = error.body as { detail?: string } | undefined;
    return body?.detail ?? error.message;
  }
  return errorMessage(error);
}

function PreviewView({ preview }: { preview: SendPreview }) {
  const email = preview.channel === "EMAIL";
  return (
    <dl className="send-preview">
      <dt>To</dt>
      <dd>
        {preview.recipient || <span className="muted">No recipient</span>}{" "}
        {preview.recipient_source === "post" && <Badge tone="success">found in the post</Badge>}
        {preview.recipient_source === "edited" && <Badge tone="warning">not in the post - check it</Badge>}
      </dd>
      {email && (
        <>
          <dt>From</dt>
          <dd>{preview.sender || <span className="muted">No email account connected</span>}</dd>
          <dt>Subject</dt>
          <dd>{preview.subject || <span className="muted">No subject</span>}</dd>
        </>
      )}
      <dt>Message</dt>
      <dd>
        <pre className="send-preview__body">{preview.body || "—"}</pre>
      </dd>
      <dt>Resume</dt>
      <dd>
        {preview.attachment.name ? (
          <>
            {preview.attachment.name}
            {preview.attachment.size ? ` (${formatBytes(preview.attachment.size)})` : ""}{" "}
            {preview.attachment.ok ? (
              <Badge tone="success">{email ? "attached" : "attach it in WhatsApp"}</Badge>
            ) : (
              <Badge tone="danger">{preview.attachment.problem}</Badge>
            )}
          </>
        ) : (
          <span className="muted">{preview.attachment.problem || "No resume selected"}</span>
        )}
      </dd>
    </dl>
  );
}

/** The preview and the ways to send one channel. Closes once the message is opened (or sent). */
function SendDialog({ application, channel, onClose }: { application: ApplicationSummary; channel: SendChannel; onClose: () => void }) {
  const api = useApi();
  const { notify } = useToast();
  const refresh = useRefresh(application.id);
  const [direct, setDirect] = useState(false);
  const checkboxId = useId();
  const email = channel === "EMAIL";
  const preview = useQuery({
    queryKey: [...sendingKeys.preview(application.id, channel), application.updated_at],
    queryFn: ({ signal }) => getSendPreview(api, application.id, channel, signal),
  });
  const account = useQuery({ queryKey: sendingKeys.account, queryFn: ({ signal }) => getSendingAccount(api, signal) });
  const reserved = useRef<ReservedWindow | null>(null);

  const compose = useMutation({
    mutationFn: ({ method, version }: { method: HandoffMethod; version: string }) =>
      openCompose(api, application.id, channel, method, version),
    onSuccess: (result) => {
      refresh(result);
      const { record } = result;
      if (record.provider === "eml_file" && result.download_url) {
        downloadFile(api.url(result.download_url));
        notify("Draft downloaded. Open it in your email app, check it and press Send there.", "success");
      } else if (record.provider === "mailbox_draft") {
        const opened = record.open_url ? reserved.current?.go(record.open_url) : false;
        if (!opened) reserved.current?.cancel();
        notify(
          record.details.mailbox === "local"
            ? "Test mode: the draft was saved in the local test mailbox, not in your real mailbox."
            : `Draft saved in your ${record.details.folder ?? "Drafts"} folder. Open it, check it and press Send there.`,
          "success",
        );
      } else if (!reserved.current?.go(record.open_url)) {
        notify("Could not open WhatsApp automatically. Use “Open WhatsApp again”.", "warning");
      }
      reserved.current = null;
      onClose();
    },
    onError: (error) => {
      reserved.current?.cancel();
      reserved.current = null;
      refresh();
      notify(failureText(error), "danger");
    },
  });
  const send = useMutation({
    mutationFn: (version: string) => sendEmail(api, application.id, version),
    onSuccess: (result) => {
      refresh(result);
      notify(`Email sent to ${result.record.recipient}.`, "success");
      onClose();
    },
    onError: (error) => {
      refresh();
      notify(`Sending failed: ${failureText(error)}`, "danger");
      onClose();
    },
  });

  function start(method: HandoffMethod, version: string) {
    // Reserve the tab inside the click (pop-up blockers), only when a page will open.
    const page = method === "link" || (method === "mailbox" && Boolean(account.data?.account?.drafts_page));
    reserved.current = page ? reserveWindow() : null;
    compose.mutate({ method, version });
  }

  const data = preview.data;
  const busy = compose.isPending || send.isPending;
  const testMode = account.data ? !deliversForReal(account.data.mode) : false;

  return (
    <Dialog open wide title={email ? "Send by email" : "Send by WhatsApp"} onClose={busy ? () => undefined : onClose}>
      {preview.error && !data && <ErrorState title="Could not prepare the message" error={preview.error} onRetry={() => void preview.refetch()} />}
      {!data && !preview.error && <Skeleton lines={4} label="Preparing the message" />}
      {data && (
        <div className="send-dialog">
          <PreviewView preview={data} />
          {data.notes.map((note) => (
            <p key={note} className="muted small">
              {note}
            </p>
          ))}
          {data.problems.length > 0 && (
            <ul className="send-problems" aria-label="Before you can send">
              {data.problems.map((problem) => (
                <li key={problem}>
                  {problem}
                  {problem.startsWith("Connect your email") && (
                    <>
                      {" "}
                      <Link to="/account#email-sending">Connect it now</Link>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
          {email ? (
            <>
              <div className="send-options">
                <div className="send-option">
                  <Button variant="primary" disabled={!data.ready || busy} loading={compose.isPending && compose.variables?.method === "mailbox"} onClick={() => start("mailbox", data.version)}>
                    Open as a draft in my email
                  </Button>
                  <span className="muted small">
                    Saved in your mailbox's Drafts folder with the resume attached
                    {account.data?.account?.drafts_page ? ", then your drafts open" : ""}. Check or edit it and press Send there.
                  </span>
                </div>
                <div className="send-option">
                  <Button disabled={!data.ready || busy} loading={compose.isPending && compose.variables?.method === "eml"} onClick={() => start("eml", data.version)}>
                    Download draft (.eml)
                  </Button>
                  <span className="muted small">For Outlook, Thunderbird or Apple Mail on this computer: the file opens as a new email, resume attached.</span>
                </div>
              </div>
              <details className="send-direct">
                <summary>Or send it directly from ApplyForge</summary>
                {testMode && <p className="notice notice--warning">Test mode: this server keeps the email locally instead of delivering it.</p>}
                <label className="checkbox" htmlFor={checkboxId}>
                  <input id={checkboxId} type="checkbox" checked={direct} onChange={(e) => setDirect(e.target.checked)} />
                  <span>I checked the recipient, the message and the resume, and I want ApplyForge to send it now.</span>
                </label>
                <Button variant="primary" disabled={!data.ready || !direct || busy} loading={send.isPending} onClick={() => send.mutate(data.version)}>
                  Send email now
                </Button>
              </details>
            </>
          ) : (
            <div className="form__actions send-actions">
              {data.attachment.resume_id && data.attachment.ok && (
                <a className="btn btn--sm" href={api.url(`resumes/${data.attachment.resume_id}/download/`)} download>
                  Download resume
                </a>
              )}
              <Button variant="primary" disabled={!data.ready || busy} loading={compose.isPending} onClick={() => start("link", data.version)}>
                Open WhatsApp
              </Button>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}

function openText(state: ChannelSendState) {
  if (state.provider === "mailbox_draft") return "Email draft saved in your mailbox";
  if (state.provider === "eml_file") return "Email draft downloaded";
  return "WhatsApp opened with your message";
}

/** One channel of one application: start, the open hand-off (confirm / check / not sent), or sent. */
export function ChannelSend({ application, channel }: { application: ApplicationSummary; channel: SendChannel }) {
  const api = useApi();
  const { notify } = useToast();
  const refresh = useRefresh(application.id);
  const [starting, setStarting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const states = application.send_state ?? { EMAIL: NO_STATE, WHATSAPP: NO_STATE };
  const state = states[channel] ?? NO_STATE;
  const other = states[channel === "EMAIL" ? "WHATSAPP" : "EMAIL"] ?? NO_STATE;
  const name = CHANNEL_NAME[channel];
  const record = state.record ?? 0;

  const confirm = useMutation({
    mutationFn: () => confirmHandoff(api, application.id, record),
    onSuccess: (result) => {
      setConfirming(false);
      refresh(result);
      notify(`Marked as sent by ${name.toLowerCase()}.`, "success");
    },
    onError: (error) => {
      setConfirming(false);
      refresh();
      notify(errorMessage(error), "danger");
    },
  });
  const verify = useMutation({
    mutationFn: () => verifyHandoff(api, application.id, record),
    onSuccess: (result) => {
      refresh(result);
      if (result.verified) notify("Found in your Sent folder: marked as sent.", "success");
      else notify(result.detail || "Not found in your Sent folder yet.", "neutral");
    },
    onError: (error) => notify(failureText(error), "danger"),
  });
  const abandon = useMutation({
    mutationFn: () => abandonHandoff(api, application.id, record),
    onSuccess: (result) => {
      refresh(result);
      notify("Marked as not sent. You can open it again.", "success");
    },
    onError: (error) => {
      refresh();
      notify(errorMessage(error), "danger");
    },
  });

  const sentBefore = ["SENT", "FOLLOW_UP"].includes(application.status);
  const startable =
    state.state !== "sent" && other.state !== "open" && (["READY_TO_SEND", "SEND_FAILED"].includes(application.status) || sentBefore);
  const hint = APPROVABLE.includes(application.status) ? "Approve the drafts first." : other.state === "open" ? `Finish the ${CHANNEL_NAME[channel === "EMAIL" ? "WHATSAPP" : "EMAIL"]} first.` : "";

  return (
    <div className="channel-send" role="group" aria-label={name}>
      <div className="channel-send__head">
        <strong>{name}</strong>
        {state.state === "sent" && <Badge tone="success">Sent</Badge>}
        {state.state === "open" && <Badge tone="warning">Opened, not sent yet</Badge>}
        {state.state === "failed" && <Badge tone="danger">Failed</Badge>}
      </div>

      {state.state === "sent" && (
        <p className="small" role="status">
          Sent by {name.toLowerCase()}
          {state.recipient ? ` to ${state.recipient}` : ""} · {formatDateTime(state.at)} · {SENT_HOW[state.status] ?? "sent"}
        </p>
      )}

      {state.state === "open" && (
        <>
          <p className="small">
            {openText(state)} {formatDateTime(state.at)}
            {state.recipient ? ` (to ${state.recipient})` : ""}. Nothing is sent until you press Send there; it is not marked as sent
            until {state.can_verify ? "ApplyForge finds it in your Sent folder or " : ""}you confirm.
          </p>
          <div className="form__actions channel-send__actions">
            {state.provider === "mailbox_draft" && state.open_url && (
              <a className="btn btn--sm" href={state.open_url} target="_blank" rel="noopener noreferrer">
                Open my drafts ↗
              </a>
            )}
            {state.provider === "eml_file" && state.record && (
              <a className="btn btn--sm" href={draftFileUrl(api, application.id, state.record)} download>
                Download the draft again
              </a>
            )}
            {state.provider === "whatsapp_link" && (
              <>
                {application.resume && (
                  <a className="btn btn--sm" href={api.url(`resumes/${application.resume}/download/`)} download>
                    Download resume
                  </a>
                )}
                {state.open_url && (
                  <a className="btn btn--sm" href={state.open_url} target="_blank" rel="noopener noreferrer">
                    Open WhatsApp again ↗
                  </a>
                )}
              </>
            )}
            {state.can_verify && (
              <Button size="sm" loading={verify.isPending} onClick={() => verify.mutate()}>
                Check my Sent folder
              </Button>
            )}
            <Button size="sm" variant="primary" onClick={() => setConfirming(true)}>
              I sent it
            </Button>
            <Button size="sm" variant="ghost" loading={abandon.isPending} onClick={() => abandon.mutate()}>
              I didn't send it
            </Button>
          </div>
          <ConfirmDialog
            open={confirming}
            title={`Did you send the ${channel === "EMAIL" ? "email" : "WhatsApp message"}?`}
            message={`Only confirm after you pressed Send in ${channel === "EMAIL" ? "your email app" : "WhatsApp"}${state.recipient ? ` (to ${state.recipient})` : ""}. The application is then marked as sent.`}
            confirmLabel="Yes, I sent it"
            onConfirm={() => confirm.mutate()}
            onCancel={() => setConfirming(false)}
          />
        </>
      )}

      {(state.state === "none" || state.state === "failed") && (
        <>
          {state.state === "failed" && state.error && (
            <p className="notice notice--error small" role="alert">
              {state.error}
            </p>
          )}
          <div className="form__actions channel-send__actions">
            <Button size="sm" variant={channel === "EMAIL" ? "primary" : "secondary"} disabled={!startable} onClick={() => setStarting(true)}>
              {state.state === "failed" ? `Try ${name} again…` : `Send ${name}…`}
            </Button>
            {!startable && hint && <span className="muted small">{hint}</span>}
          </div>
        </>
      )}
      {starting && <SendDialog application={application} channel={channel} onClose={() => setStarting(false)} />}
    </div>
  );
}

export function ApproveDrafts({ application }: { application: ApplicationSummary }) {
  const api = useApi();
  const { notify } = useToast();
  const refresh = useRefresh(application.id);
  const queryClient = useQueryClient();
  const approve = useMutation({
    mutationFn: () => updateApplication(api, application.id, { status: "READY_TO_SEND", note: "Drafts approved for sending." }),
    onSuccess: (updated) => {
      queryClient.setQueryData(applicationKeys.detail(application.id), updated);
      refresh();
      notify("Approved. You can send it now.", "success");
    },
    onError: (error) => notify(errorMessage(error), "danger"),
  });
  if (!APPROVABLE.includes(application.status)) return null;
  return (
    <div className="notice notice--info send-approve">
      <span>Read the drafts. When they are right, approve them to send.</span>
      <Button size="sm" variant="primary" loading={approve.isPending} onClick={() => approve.mutate()}>
        Approve drafts
      </Button>
    </div>
  );
}

export function SendPanel({ application }: { application: ApplicationDetail }) {
  const api = useApi();
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    // Opened from a "Review and send" link (#send): bring the panel into view once.
    if (window.location.hash === "#send") panel.current?.scrollIntoView?.({ block: "start" });
  }, []);
  const records = useQuery({
    queryKey: [...sendingKeys.records(application.id), application.updated_at],
    queryFn: ({ signal }) => listSendRecords(api, application.id, signal),
  });

  return (
    <section className="panel" id="send" ref={panel} aria-labelledby="send-title">
      <div>
        <h2 id="send-title" className="panel__title">
          Send
        </h2>
        <p className="panel__subtitle">
          You send it yourself from your email or WhatsApp; ApplyForge prepares it (with your resume) and tracks it.
        </p>
      </div>
      <ApproveDrafts application={application} />
      <div className="channel-send-list">
        <ChannelSend application={application} channel="EMAIL" />
        <ChannelSend application={application} channel="WHATSAPP" />
      </div>

      {(records.data?.length ?? 0) > 0 && (
        <div className="send-records">
          <h3 className="panel__title panel__title--small">Sending history</h3>
          <ul className="plain-list">
            {records.data!.map((record) => (
              <li key={record.id} className="send-record">
                <Badge tone={recordTone(record.status)}>{RECORD_LABEL[record.status] ?? record.status}</Badge>{" "}
                {record.channel === "EMAIL" ? "Email" : "WhatsApp"} to {record.recipient} · {formatDateTime(record.created_at)}
                {record.finished_at && record.status !== "FAILED" && <span className="muted"> → {formatDateTime(record.finished_at)}</span>}
                {record.attachment_name && <span className="muted"> · {record.attachment_name}</span>}
                {record.error && <span className="send-record__error"> - {record.error}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
