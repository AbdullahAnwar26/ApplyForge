/**
 * Step 3 - Apply. One card per written application:
 *
 * - "Send email": one click. ApplyForge puts the finished email - recruiter's address,
 *   subject, message and the resume attached - in the Drafts of the user's own Gmail and
 *   opens it; the user presses Send in Gmail. Coming back to ApplyForge, the Sent folder
 *   is checked automatically, so the card turns "Sent" by itself (or "I sent it").
 * - "Send WhatsApp" (only when the post has a number): one click downloads the resume and
 *   opens the recruiter's chat with the message typed in; the user attaches the resume
 *   and presses send, then "I sent it". (No link can attach a file in WhatsApp.)
 *
 * Opening is never sending: nothing leaves without the user pressing Send themselves.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { applicationKeys, getApplication, listApplicationsInStatuses } from "../../api/applications";
import { isApiError } from "../../api/errors";
import { jobKeys } from "../../api/jobs";
import { abandonHandoff, confirmHandoff, deliversForReal, getSendingAccount, openCompose, sendingKeys, verifyHandoff } from "../../api/sending";
import type { ApplicationStatus, ApplicationSummary, ChannelSendState, Profile, SendChannel, SendResult } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { EmptyState, ErrorState, errorMessage, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { downloadFile, reserveWindow } from "../../lib/external";
import { formatDateTime } from "../../lib/format";

export const APPLY_STATUSES: ApplicationStatus[] = ["GENERATED", "OPENED", "READY_TO_SEND", "SEND_FAILED", "COMPOSE_OPENED"];
const SENT_STATUSES: ApplicationStatus[] = ["SENT", "FOLLOW_UP"];
const RESUME_KEY = (id: number) => `applyforge.resume-downloaded.${id}`;

function resumeDownloaded(id: number): boolean {
  try {
    return window.localStorage.getItem(RESUME_KEY(id)) === "1";
  } catch {
    return false;
  }
}

function rememberResumeDownload(id: number) {
  try {
    window.localStorage.setItem(RESUME_KEY(id), "1");
  } catch {
    /* private mode: it is downloaded again next time, which is harmless */
  }
}

const NONE: ChannelSendState = { state: "none", record: null, status: "", provider: "", recipient: "", at: null, open_url: "", can_verify: false, error: "" };

function failure(error: unknown) {
  if (isApiError(error)) {
    const body = error.body as Record<string, unknown> | undefined;
    if (body && typeof body.detail === "string") return body.detail;
  }
  return errorMessage(error);
}

function ChannelButton({ application, channel, hasMessage }: { application: ApplicationSummary; channel: SendChannel; hasMessage: boolean }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const account = useQuery({ queryKey: sendingKeys.account, queryFn: ({ signal }) => getSendingAccount(api, signal) });
  const state = application.send_state?.[channel] ?? NONE;
  const email = channel === "EMAIL";
  // WhatsApp: the exact number the link opens (country code resolved by the server).
  const whatsapp = application.whatsapp ?? { number: application.recipient_phone ?? "", note: "", problem: "" };
  const recipient = email ? application.recipient_email : whatsapp.number || application.recipient_phone;
  const record = state.record ?? 0;

  const done = (result?: SendResult) => {
    if (result?.application) queryClient.setQueryData(applicationKeys.detail(application.id), result.application);
    void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
    void queryClient.invalidateQueries({ queryKey: jobKeys.all });
  };
  const open = useMutation({
    mutationFn: () => openCompose(api, application.id, channel, email ? "mailbox" : "link", ""),
    onSuccess: (result) => done(result),
    onError: (error) => {
      done();
      notify(failure(error), "danger");
    },
  });
  const sent = useMutation({
    mutationFn: () => confirmHandoff(api, application.id, record),
    onSuccess: done,
    onError: (error) => notify(errorMessage(error), "danger"),
  });
  const notSent = useMutation({
    mutationFn: () => abandonHandoff(api, application.id, record),
    onSuccess: done,
    onError: (error) => notify(errorMessage(error), "danger"),
  });
  const check = useMutation({
    mutationFn: () => verifyHandoff(api, application.id, record),
    onSuccess: (result) => {
      if (result.verified) {
        done(result);
        notify(`Email to ${state.recipient} found in your Sent folder.`, "success");
      }
    },
  });

  // Back from Gmail: look in the Sent folder by itself (read only).
  const checkSent = check.mutate; // stable between renders
  useEffect(() => {
    if (!(state.state === "open" && state.can_verify)) return;
    const onFocus = () => checkSent();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [state.state, state.can_verify, state.record, checkSent]);

  function start() {
    const gmailPage = Boolean(account.data?.account?.drafts_page);
    const tab = !email || gmailPage ? reserveWindow() : null; // inside the click: pop-up blockers
    // The resume is downloaded once per browser (it is the same file for every chat).
    if (!email && application.resume && !resumeDownloaded(application.resume)) {
      downloadFile(api.url(`resumes/${application.resume}/download/`));
      rememberResumeDownload(application.resume);
    }
    open.mutate(undefined, {
      onSuccess: (result) => {
        const url = result.record.open_url;
        if (url && tab?.go(url)) return;
        tab?.cancel();
        if (email && result.record.details.mailbox === "local") {
          notify("Test mode: the email was saved in the local test mailbox (not your real Gmail).", "neutral");
        } else if (email) {
          notify("The email is in your Drafts with the resume attached. Open it and press Send.", "success");
        } else {
          notify("Allow pop-ups for this site, or use “Open again”.", "warning");
        }
      },
      onError: () => tab?.cancel(),
    });
  }

  if (!recipient || (!email && !hasMessage)) return null;

  if (state.state === "sent") {
    return (
      <p className="apply-channel apply-channel--sent" role="status">
        ✓ {email ? "Email" : "WhatsApp"} sent {formatDateTime(state.at)}
        {state.status === "VERIFIED" && <span className="muted"> (found in your Sent folder)</span>}
      </p>
    );
  }
  if (state.state === "open") {
    return (
      <div className="apply-channel apply-channel--open">
        <span className="small">
          {email ? "Opened in your email" : "Opened in WhatsApp"}: press Send there
          {email ? "" : " (attach your resume from your Downloads folder)"}.{" "}
          {email && state.can_verify ? "This turns Sent by itself when you come back." : ""}
        </span>
        <div className="apply-channel__actions">
          {state.open_url && (
            <a className="btn btn--sm" href={state.open_url} target="_blank" rel="noopener noreferrer">
              Open again ↗
            </a>
          )}
          {!email && application.resume && (
            <a className="btn btn--sm" href={api.url(`resumes/${application.resume}/download/`)} download>
              Download resume again
            </a>
          )}
          <Button size="sm" variant="primary" loading={sent.isPending} onClick={() => sent.mutate()}>
            I sent it
          </Button>
          <Button size="sm" variant="ghost" loading={notSent.isPending} onClick={() => notSent.mutate()}>
            Not sent
          </Button>
        </div>
      </div>
    );
  }
  if (email && account.data?.account?.app_password_ok === false) {
    return (
      <p className="apply-channel small notice notice--warning">
        Gmail will refuse your saved password: it is not a Google app password (16 letters).{" "}
        <Link to="/account#email-sending">Replace it with an app password</Link>
      </p>
    );
  }
  const needsAccount = email && account.data && !account.data.account;
  if (needsAccount) {
    return (
      <p className="apply-channel small">
        To send emails with your resume attached, <Link to="/account#email-sending">connect your Gmail once</Link>.
      </p>
    );
  }
  if (!email && whatsapp.problem) {
    return (
      <p className="apply-channel small">
        WhatsApp {application.recipient_phone}: {whatsapp.problem} <Link to="/profile">Set your country code</Link>
      </p>
    );
  }
  return (
    <div className="apply-channel">
      <Button variant={email ? "primary" : "secondary"} loading={open.isPending} onClick={start}>
        {email ? "Send email" : "Send WhatsApp"}
      </Button>
      <span className="muted small">
        to {recipient}
        {state.state === "failed" && state.error ? ` · last try failed: ${state.error}` : ""}
      </span>
      {!email && whatsapp.note && <span className="muted small apply-channel__note">{whatsapp.note}</span>}
    </div>
  );
}

function ApplyCard({ application }: { application: ApplicationSummary }) {
  const api = useApi();
  const [showEmail, setShowEmail] = useState(false);
  const detail = useQuery({
    queryKey: applicationKeys.detail(application.id),
    queryFn: ({ signal }) => getApplication(api, application.id, signal),
  });
  const d = detail.data;
  const hasWhatsApp = Boolean(d?.generated_whatsapp_message && application.recipient_phone);

  return (
    <li className="apply-card">
      <div className="apply-card__head">
        <div className="apply-card__title">
          <Link to={`/applications/${application.id}`}>{application.job.title}</Link>
          <span className="muted small">{application.job.company?.name ?? "Company not stated"}</span>
        </div>
        {application.status === "SEND_FAILED" && <Badge tone="danger">Last try failed</Badge>}
      </div>
      {d ? (
        <div className="apply-card__email">
          <div className="small">
            <strong>{d.generated_subject || "No subject"}</strong>
            <span className="muted"> · to {application.recipient_email || "no email in the post"}</span>
          </div>
          {showEmail && <pre className="send-preview__body">{d.generated_body}</pre>}
          {showEmail && hasWhatsApp && (
            <>
              <div className="small muted">WhatsApp message</div>
              <pre className="send-preview__body">{d.generated_whatsapp_message}</pre>
            </>
          )}
          <span className="small">
            <button type="button" className="link-button" onClick={() => setShowEmail((v) => !v)} aria-expanded={showEmail}>
              {showEmail ? "Hide message" : "Read message"}
            </button>
            {" · "}
            <Link to={`/applications/${application.id}`}>Edit</Link>
          </span>
        </div>
      ) : (
        <Skeleton lines={1} label="Loading the message" />
      )}
      <div className="apply-card__actions">
        <ChannelButton application={application} channel="EMAIL" hasMessage />
        <ChannelButton application={application} channel="WHATSAPP" hasMessage={hasWhatsApp} />
        {!application.recipient_email && !application.recipient_phone && (
          <p className="small muted">The post has no email or phone. Add one on the job page to send.</p>
        )}
      </div>
    </li>
  );
}

export function ApplyStep({ profile }: { profile: Profile }) {
  const api = useApi();
  const account = useQuery({ queryKey: sendingKeys.account, queryFn: ({ signal }) => getSendingAccount(api, signal) });
  const toSend = useQuery({
    queryKey: [...applicationKeys.lists(), "apply", profile.id],
    queryFn: ({ signal }) => listApplicationsInStatuses(api, profile.id, APPLY_STATUSES, 1, 200, signal),
    placeholderData: keepPreviousData,
  });
  const sent = useQuery({
    queryKey: [...applicationKeys.lists(), "applied", profile.id],
    queryFn: ({ signal }) => listApplicationsInStatuses(api, profile.id, SENT_STATUSES, 1, 50, signal),
  });

  if (toSend.error && !toSend.data) return <ErrorState title="Could not load your applications" error={toSend.error} onRetry={() => void toSend.refetch()} />;
  if (!toSend.data) return <Skeleton lines={5} label="Loading applications" />;
  const items = toSend.data.items;
  const draftsMode = account.data?.drafts_mode ?? (account.data && deliversForReal(account.data.mode) ? "imap" : "local");
  const testMode = Boolean(account.data?.account) && draftsMode === "local";

  return (
    <>
      <section className="panel" aria-labelledby="apply-title">
        <div>
          <h2 id="apply-title" className="panel__title">
            Apply
          </h2>
          <p className="panel__subtitle">
            “Send email” opens the finished email in your Gmail with your resume attached: just press Send. “Send WhatsApp” opens the
            recruiter's chat with the message typed in: attach the resume that was downloaded and press send.
          </p>
        </div>
        {testMode && (
          <p className="notice notice--warning" role="note">
            Test mode: emails go to a local test mailbox on this computer, not to your real Gmail.
          </p>
        )}
        {account.data?.account && draftsMode === "imap" && (
          <p className="notice notice--info" role="note">
            Emails open as drafts in your mailbox ({account.data.account.from_email}). Nothing is sent until you press Send there.
          </p>
        )}
        {items.length === 0 ? (
          <EmptyState title="Nothing to send yet">Pick jobs in step 2 and write the applications; they appear here.</EmptyState>
        ) : (
          <ul className="apply-list" aria-label="Applications to send">
            {items.map((application) => (
              <ApplyCard key={application.id} application={application} />
            ))}
          </ul>
        )}
      </section>
      {(sent.data?.items.length ?? 0) > 0 && (
        <section className="panel" aria-labelledby="applied-title">
          <h2 id="applied-title" className="panel__title">
            Sent
          </h2>
          <ul className="apply-list" aria-label="Sent applications">
            {sent.data!.items.map((application) => (
              <ApplyCard key={application.id} application={application} />
            ))}
          </ul>
          <p className="small">
            <Link to="/applications">Track replies and follow-ups →</Link>
          </p>
        </section>
      )}
    </>
  );
}
