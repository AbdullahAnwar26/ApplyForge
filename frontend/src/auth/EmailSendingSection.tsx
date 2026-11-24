/**
 * Account -> Email sending: the mailbox the user's application emails are sent from
 * (their own account over SMTP, normally with an app password). The password is
 * write-only: the server stores it encrypted and never shows it again. A test email
 * goes to the user's own address only, and also checks that drafts can be saved in the
 * mailbox (IMAP, same login): "Send Email" opens the application as a draft there.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { deleteSendingAccount, deliversForReal, getSendingAccount, saveSendingAccount, sendingKeys, testSendingAccount } from "../api/sending";
import type { SendingAccount, SendingAccountInput, SmtpSecurity } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { Field, FormErrors } from "../components/Field";
import { ErrorState, errorMessage, Skeleton } from "../components/States";
import { useToast } from "../components/Toast";
import { formatDateTime } from "../lib/format";
import { useApi } from "./AuthProvider";

const PRESETS: { label: string; host: string; port: number; security: SmtpSecurity }[] = [
  { label: "Gmail", host: "smtp.gmail.com", port: 587, security: "starttls" },
  { label: "Outlook / Hotmail", host: "smtp-mail.outlook.com", port: 587, security: "starttls" },
  { label: "Yahoo", host: "smtp.mail.yahoo.com", port: 465, security: "ssl" },
];

function initial(account: SendingAccount | null): SendingAccountInput {
  return {
    from_name: account?.from_name ?? "",
    from_email: account?.from_email ?? "",
    smtp_host: account?.smtp_host ?? "smtp.gmail.com",
    smtp_port: account?.smtp_port ?? 587,
    security: account?.security ?? "starttls",
    username: account?.username ?? "",
    imap_host: account?.imap_host ?? "",
    imap_port: account?.imap_port ?? 993,
    password: "",
  };
}

function AccountForm({ account, mode, draftsMode }: { account: SendingAccount | null; mode: string; draftsMode: "imap" | "local" }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const [values, setValues] = useState<SendingAccountInput>(() => initial(account));
  const [removing, setRemoving] = useState(false);
  const set = (changes: Partial<SendingAccountInput>) => setValues((v) => ({ ...v, ...changes }));
  const store = (data: unknown) => queryClient.setQueryData(sendingKeys.account, data);

  const save = useMutation({
    mutationFn: () => {
      const { password, ...rest } = values;
      return saveSendingAccount(api, password ? { ...rest, password } : rest);
    },
    onSuccess: (data) => {
      store(data);
      set({ password: "" });
      void queryClient.invalidateQueries({ queryKey: ["sending", "preview"] });
      notify("Email account saved. Send a test email to check it.", "success");
    },
  });
  const test = useMutation({
    mutationFn: () => testSendingAccount(api),
    onSuccess: (data) => {
      store(data);
      if (data.drafts && !data.drafts.ok) {
        notify(`Test email sent, but drafts cannot be saved in your mailbox: ${data.drafts.error}`, "warning");
      } else {
        notify(
          `Test email sent to ${data.account?.from_email}.${data.drafts?.folders.drafts ? ` Drafts folder found: ${data.drafts.folders.drafts}.` : ""}`,
          "success",
        );
      }
    },
    onError: (error) => {
      void queryClient.invalidateQueries({ queryKey: sendingKeys.account });
      notify(errorMessage(error), "danger");
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteSendingAccount(api),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sending"] });
      setValues(initial(null));
      notify("Email account removed.", "success");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!save.isPending) save.mutate();
  }

  return (
    <form className="form" onSubmit={submit} noValidate>
      {!deliversForReal(mode) && (
        <p className="notice notice--warning" role="note">
          {draftsMode === "imap"
            ? "“Send email” saves drafts in your real mailbox (you press Send there). Sending directly from ApplyForge is in test mode on this server."
            : `Test mode: this server keeps emails and drafts locally (${mode}) instead of using your mailbox.`}
        </p>
      )}
      {account?.verified_at && <p className="notice notice--success">Working: test email sent {formatDateTime(account.verified_at)}.</p>}
      {account?.last_error && (
        <p className="notice notice--error" role="alert">
          Last test failed: {account.last_error}
        </p>
      )}
      <p className="muted small">
        Quick setup:{" "}
        {PRESETS.map((preset, index) => (
          <span key={preset.label}>
            {index > 0 && " · "}
            <button
              type="button"
              className="link-button"
              onClick={() => set({ smtp_host: preset.host, smtp_port: preset.port, security: preset.security })}
            >
              {preset.label}
            </button>
          </span>
        ))}
        . Gmail and Outlook need an <strong>app password</strong> (created in your Google or Microsoft account security
        settings), not your normal password.
      </p>
      <div className="form__grid">
        <Field label="Your name (shown as sender)">
          {(p) => <input {...p} className="input" value={values.from_name} onChange={(e) => set({ from_name: e.target.value })} />}
        </Field>
        <Field label="Your email address" required>
          {(p) => (
            <input
              {...p}
              className="input"
              type="email"
              value={values.from_email}
              onChange={(e) =>
                // The username follows the address until the user changes it.
                set({
                  from_email: e.target.value,
                  username: !values.username || values.username === values.from_email ? e.target.value : values.username,
                })
              }
            />
          )}
        </Field>
        <Field label="SMTP server" required>
          {(p) => <input {...p} className="input" value={values.smtp_host} onChange={(e) => set({ smtp_host: e.target.value })} />}
        </Field>
        <Field label="Port" required>
          {(p) => (
            <input {...p} className="input" type="number" value={values.smtp_port} onChange={(e) => set({ smtp_port: Number(e.target.value) })} />
          )}
        </Field>
        <Field label="Security">
          {(p) => (
            <select {...p} className="input" value={values.security} onChange={(e) => set({ security: e.target.value as SmtpSecurity })}>
              <option value="starttls">STARTTLS (usually port 587)</option>
              <option value="ssl">SSL/TLS (usually port 465)</option>
            </select>
          )}
        </Field>
        <Field label="Username" required hint="Usually your email address.">
          {(p) => <input {...p} className="input" autoComplete="off" value={values.username} onChange={(e) => set({ username: e.target.value })} />}
        </Field>
        <Field
          label={account?.has_password ? "New app password" : "App password"}
          required={!account?.has_password}
          hint={account?.has_password ? "Saved. Leave empty to keep it." : "Stored encrypted; never shown again."}
        >
          {(p) => (
            <input
              {...p}
              className="input"
              type="password"
              autoComplete="new-password"
              value={values.password ?? ""}
              onChange={(e) => set({ password: e.target.value })}
            />
          )}
        </Field>
      </div>
      <details className="form__advanced">
        <summary>Drafts (IMAP) settings</summary>
        <p className="muted small">
          “Send Email” saves the application as a draft in your mailbox with the same login (IMAP must be enabled for your
          account).
        </p>
        <div className="form__grid">
          <Field label="IMAP server" hint={`Leave empty to use ${account?.drafts_server || "the one matching your SMTP server"}.`}>
            {(p) => <input {...p} className="input" value={values.imap_host ?? ""} onChange={(e) => set({ imap_host: e.target.value })} />}
          </Field>
          <Field label="IMAP port">
            {(p) => (
              <input {...p} className="input" type="number" value={values.imap_port ?? 993} onChange={(e) => set({ imap_port: Number(e.target.value) })} />
            )}
          </Field>
        </div>
      </details>
      <FormErrors error={save.error} />
      <div className="form__actions">
        {account && (
          <Button type="button" variant="ghost" onClick={() => setRemoving(true)}>
            Remove
          </Button>
        )}
        {account && (
          <Button type="button" loading={test.isPending} onClick={() => test.mutate()}>
            Send a test email to myself
          </Button>
        )}
        <Button type="submit" variant="primary" loading={save.isPending}>
          Save
        </Button>
      </div>
      <ConfirmDialog
        open={removing}
        title="Remove your email account?"
        message="ApplyForge will no longer be able to send emails for you. You can connect it again at any time."
        confirmLabel="Remove"
        tone="danger"
        onConfirm={() => {
          setRemoving(false);
          remove.mutate();
        }}
        onCancel={() => setRemoving(false)}
      />
    </form>
  );
}

export function EmailSendingSection() {
  const api = useApi();
  const state = useQuery({ queryKey: sendingKeys.account, queryFn: ({ signal }) => getSendingAccount(api, signal) });
  return (
    <section className="panel" id="email-sending" aria-labelledby="email-sending-title">
      <div>
        <h2 id="email-sending-title" className="panel__title">
          Email sending
        </h2>
        <p className="panel__subtitle">
          Applications you approve are sent from your own mailbox, with your resume attached. Only you can use it.
        </p>
      </div>
      {state.error && !state.data ? (
        <ErrorState error={state.error} title="Could not load your email settings" onRetry={() => void state.refetch()} />
      ) : !state.data ? (
        <Skeleton lines={4} label="Loading email settings" />
      ) : (
        <AccountForm
          key={state.data.account?.updated_at ?? "new"}
          account={state.data.account}
          mode={state.data.mode}
          draftsMode={state.data.drafts_mode ?? (deliversForReal(state.data.mode) ? "imap" : "local")}
        />
      )}
    </section>
  );
}
