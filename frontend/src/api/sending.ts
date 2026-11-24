/**
 * Sending API (backend/tracker/api/sending_views.py, tracker/sending.py):
 *
 *   GET/PUT/DELETE sending/email-account/        the user's own mailbox (password write-only)
 *   POST sending/email-account/test/             test email to the user's own address
 *   GET  applications/{id}/send-preview/?channel exactly what would be sent + its `version`
 *   POST applications/{id}/send/                 {channel: "EMAIL", version, confirm: true}
 *                                                200 sent / 502 failed (recorded, retry allowed)
 *                                                400 refused / 409 changed since the preview
 *   POST applications/{id}/whatsapp-sent/        {version, confirm: true} after sending from WhatsApp
 *   GET  applications/{id}/send-records/         every attempt, newest first
 *
 * Hand-off (you send it from your own email app or WhatsApp; opening never marks it sent):
 *   POST applications/{id}/compose/              {channel, method, version} -> record COMPOSE_OPENED
 *   POST applications/{id}/compose/confirm/      {record, confirm: true}    you sent it
 *   POST applications/{id}/compose/verify/       {record}                   look in your Sent folder
 *   POST applications/{id}/compose/abandon/      {record}                   you did not send it
 *   GET  applications/{id}/compose/draft/?record the .eml draft (resume attached)
 *
 * Nothing is sent without the user's explicit confirmation; these POSTs are never retried.
 */

import type { ApiClient } from "./client";
import type {
  ApplicationChannel,
  HandoffMethod,
  SendChannel,
  SendingAccountInput,
  SendingAccountState,
  SendPreview,
  SendRecord,
  SendResult,
} from "./types";

export const sendingKeys = {
  account: ["sending", "account"] as const,
  preview: (id: number, channel: ApplicationChannel) => ["sending", "preview", id, channel] as const,
  records: (id: number) => ["sending", "records", id] as const,
};

export function getSendingAccount(api: ApiClient, signal?: AbortSignal): Promise<SendingAccountState> {
  return api.get<SendingAccountState>("sending/email-account/", { signal });
}

export function saveSendingAccount(api: ApiClient, input: SendingAccountInput): Promise<SendingAccountState> {
  return api.put<SendingAccountState>("sending/email-account/", input);
}

export function deleteSendingAccount(api: ApiClient): Promise<void> {
  return api.delete<void>("sending/email-account/");
}

export function testSendingAccount(api: ApiClient): Promise<SendingAccountState & { ok: boolean }> {
  return api.post<SendingAccountState & { ok: boolean }>("sending/email-account/test/", {});
}

export function getSendPreview(api: ApiClient, id: number, channel: ApplicationChannel, signal?: AbortSignal): Promise<SendPreview> {
  return api.get<SendPreview>(`applications/${id}/send-preview/`, { query: { channel }, signal });
}

export function sendEmail(api: ApiClient, id: number, version: string): Promise<SendResult> {
  return api.post<SendResult>(`applications/${id}/send/`, { channel: "EMAIL", version, confirm: true });
}

export function confirmWhatsAppSent(api: ApiClient, id: number, version: string): Promise<SendResult> {
  return api.post<SendResult>(`applications/${id}/whatsapp-sent/`, { version, confirm: true });
}

export function openCompose(api: ApiClient, id: number, channel: SendChannel, method: HandoffMethod, version: string): Promise<SendResult> {
  return api.post<SendResult>(`applications/${id}/compose/`, { channel, method, version });
}

export function confirmHandoff(api: ApiClient, id: number, record: number): Promise<SendResult> {
  return api.post<SendResult>(`applications/${id}/compose/confirm/`, { record, confirm: true });
}

export function verifyHandoff(api: ApiClient, id: number, record: number): Promise<SendResult> {
  return api.post<SendResult>(`applications/${id}/compose/verify/`, { record });
}

export function abandonHandoff(api: ApiClient, id: number, record: number): Promise<SendResult> {
  return api.post<SendResult>(`applications/${id}/compose/abandon/`, { record });
}

export function draftFileUrl(api: ApiClient, id: number, record: number): string {
  return api.url(`applications/${id}/compose/draft/?record=${record}`);
}

export function listSendRecords(api: ApiClient, id: number, signal?: AbortSignal): Promise<SendRecord[]> {
  return api.get<SendRecord[]>(`applications/${id}/send-records/`, { signal });
}

/** "smtp" delivers for real; any other backend keeps messages on the server. */
export const deliversForReal = (mode: string): boolean => mode === "smtp";
