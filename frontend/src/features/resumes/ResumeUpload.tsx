/**
 * Resume upload: PDF or DOCX, up to 10 MB (backend/tracker/resume_text.py).
 * Type, size and emptiness are checked here first; the backend still checks the
 * content (valid PDF/DOCX, not password-protected, contains text) and its
 * message is shown next to the file field.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState, type DragEvent, type FormEvent } from "react";

import { isApiError } from "../../api/errors";
import { resumeKeys, uploadResume } from "../../api/resources";
import type { Resume } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Button } from "../../components/Button";
import { Field, FormErrors } from "../../components/Field";
import { useToast } from "../../components/Toast";
import { formatBytes } from "../../lib/format";

export const MAX_RESUME_BYTES = 10 * 1024 * 1024;
export const ACCEPTED_EXTENSIONS = [".pdf", ".docx"];

/** A reason the file cannot be uploaded, or null. */
export function checkResumeFile(file: File): string | null {
  const lower = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "Choose a PDF or DOCX file.";
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_RESUME_BYTES) return `The file is ${formatBytes(file.size)}; the maximum is 10 MB.`;
  return null;
}

function isAbort(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

export function ResumeUpload({ profileId, hasDefault }: { profileId: number; hasDefault: boolean }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [resumeType, setResumeType] = useState("");
  const [makeDefault, setMakeDefault] = useState(!hasDefault);
  const [progress, setProgress] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileId = useId();
  const fileHintId = useId();
  const fileErrorId = useId();

  const mutation = useMutation({
    mutationFn: (input: { file: File; name: string; resumeType: string; isDefault: boolean }) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      setProgress(0);
      return uploadResume(
        api,
        { profile: profileId, ...input },
        { onProgress: setProgress, signal: controller.signal },
      );
    },
    onSuccess: (resume) => {
      queryClient.setQueryData<Resume[]>(resumeKeys.forProfile(profileId), (current = []) => [
        ...current.map((r) => (resume.is_default ? { ...r, is_default: false } : r)),
        resume,
      ]);
      void queryClient.invalidateQueries({ queryKey: resumeKeys.forProfile(profileId) });
      notify(`Uploaded ${resume.name}`, "success");
      setFile(null);
      setName("");
      setResumeType("");
      setMakeDefault(false);
      if (inputRef.current) inputRef.current.value = "";
    },
    onSettled: () => {
      controllerRef.current = null;
      setProgress(null);
    },
  });

  function choose(next: File | null) {
    mutation.reset();
    setFile(next);
    setFileProblem(next ? checkResumeFile(next) : null);
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    if (mutation.isPending) return;
    const dropped = event.dataTransfer.files[0];
    if (dropped) choose(dropped);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mutation.isPending) return; // no duplicate uploads
    if (!file) return setFileProblem("Choose a resume file to upload.");
    const problem = checkResumeFile(file);
    if (problem) return setFileProblem(problem);
    mutation.mutate({ file, name, resumeType, isDefault: makeDefault });
  }

  const error = mutation.error && !isAbort(mutation.error) ? mutation.error : null;
  const fileError = fileProblem ?? (isApiError(error) ? error.fieldMessage("file") : null);
  const uploading = mutation.isPending;

  return (
    <form className="form" onSubmit={onSubmit} noValidate>
      <div className="field">
        <span className="field__label" id={`${fileId}-label`}>
          Resume file <span className="field__required" aria-hidden="true">*</span>
        </span>
        <label
          className={`dropzone${dragging ? " is-dragging" : ""}${fileError ? " is-invalid" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            if (!uploading) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={inputRef}
            id={fileId}
            type="file"
            className="dropzone__input"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            aria-labelledby={`${fileId}-label`}
            aria-describedby={[fileHintId, fileError ? fileErrorId : null].filter(Boolean).join(" ")}
            aria-invalid={fileError ? true : undefined}
            disabled={uploading}
            onChange={(event) => choose(event.target.files?.[0] ?? null)}
          />
          {file ? (
            <span className="dropzone__file">
              <strong>{file.name}</strong> <span className="muted">· {formatBytes(file.size)}</span>
            </span>
          ) : (
            <span className="dropzone__prompt">
              <strong>Choose a file</strong> or drop it here
            </span>
          )}
        </label>
        <p id={fileHintId} className="field__hint">
          PDF or DOCX, up to 10 MB, with selectable text (scanned images cannot be read).
        </p>
        {fileError && (
          <p id={fileErrorId} className="field__error" role="alert">
            {fileError}
          </p>
        )}
      </div>

      <div className="form__grid">
        <Field label="Name" hint="Optional. Defaults to the file name." error={isApiError(error) ? error.fieldMessage("name") : null}>
          {(props) => (
            <input {...props} className="input" value={name} maxLength={255} disabled={uploading} onChange={(e) => setName(e.target.value)} />
          )}
        </Field>
        <Field
          label="Resume type"
          hint="Optional label, e.g. Backend or Full-stack."
          error={isApiError(error) ? error.fieldMessage("resume_type") : null}
        >
          {(props) => (
            <input
              {...props}
              className="input"
              value={resumeType}
              maxLength={100}
              disabled={uploading}
              onChange={(e) => setResumeType(e.target.value)}
            />
          )}
        </Field>
      </div>

      <label className="checkbox">
        <input type="checkbox" checked={makeDefault} disabled={uploading} onChange={(e) => setMakeDefault(e.target.checked)} />
        <span>
          Make this the default resume <small>(used when preparing applications)</small>
        </span>
      </label>

      {progress !== null && (
        <div className="progress">
          <div
            className="progress__bar"
            role="progressbar"
            aria-label="Upload progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
          >
            <span style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
          <span className="progress__label">
            {progress < 1 ? `Uploading… ${Math.round(progress * 100)}%` : "Checking the file…"}
          </span>
        </div>
      )}

      <FormErrors error={error} shownFields={["file", "name", "resume_type"]} />
      {mutation.error && isAbort(mutation.error) && (
        <p className="notice" role="status">
          Upload cancelled. Nothing was saved.
        </p>
      )}

      <div className="form__actions">
        {uploading && <Button onClick={() => controllerRef.current?.abort()}>Cancel upload</Button>}
        <Button type="submit" variant="primary" loading={uploading} disabled={!file || Boolean(fileProblem)}>
          Upload resume
        </Button>
      </div>
    </form>
  );
}
