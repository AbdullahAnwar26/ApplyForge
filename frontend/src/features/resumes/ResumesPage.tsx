/**
 * Resumes of the active profile: list, upload, set default, activate/deactivate,
 * view extracted text, delete. The backend exposes no download URL and no way to
 * submit resume text: text is only extracted from the uploaded file.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { deleteResume, getResumeText, listResumes, resumeKeys, updateResume } from "../../api/resources";
import type { Profile, Resume } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { DataTable, type Column } from "../../components/DataTable";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState, errorMessage, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { formatDate } from "../../lib/format";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { ResumeUpload } from "./ResumeUpload";

function ResumeTextDialog({ resume, onClose }: { resume: Resume | null; onClose: () => void }) {
  const api = useApi();
  const text = useQuery({
    queryKey: resumeKeys.text(resume?.id ?? 0),
    queryFn: ({ signal }) => getResumeText(api, resume!.id, signal),
    enabled: resume !== null,
    staleTime: 60_000,
  });

  return (
    <Dialog
      open={resume !== null}
      title={resume ? `Text of ${resume.name}` : ""}
      description="Extracted from the uploaded file - this is what application drafts are based on."
      onClose={onClose}
      wide
    >
      {text.isPending && <Skeleton lines={6} label="Extracting text" />}
      {text.error && <ErrorState title="Could not extract the text" error={text.error} onRetry={() => void text.refetch()} />}
      {text.data && (
        <>
          <p className="muted">{text.data.characters.toLocaleString()} characters</p>
          {/* Plain text only: never rendered as HTML. */}
          <pre className="resume-text" tabIndex={0}>
            {text.data.text}
          </pre>
        </>
      )}
    </Dialog>
  );
}

function ResumeList({ profile }: { profile: Profile }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const [viewing, setViewing] = useState<Resume | null>(null);
  const [deleting, setDeleting] = useState<Resume | null>(null);
  const key = resumeKeys.forProfile(profile.id);

  const resumes = useQuery({ queryKey: key, queryFn: ({ signal }) => listResumes(api, profile.id, signal) });

  const update = useMutation({
    mutationFn: (input: { resume: Resume; changes: Parameters<typeof updateResume>[2]; done: string }) =>
      updateResume(api, input.resume.id, input.changes),
    onSuccess: (_, input) => {
      void queryClient.invalidateQueries({ queryKey: key });
      notify(input.done, "success");
    },
    onError: (error) => notify(errorMessage(error), "danger"),
  });

  const removal = useMutation({
    mutationFn: (resume: Resume) => deleteResume(api, resume.id),
    onSuccess: (_, resume) => {
      queryClient.setQueryData<Resume[]>(key, (current = []) => current.filter((r) => r.id !== resume.id));
      void queryClient.invalidateQueries({ queryKey: key });
      notify(`Deleted ${resume.name}`, "success");
    },
  });

  const busy = (resume: Resume) =>
    (update.isPending && update.variables?.resume.id === resume.id) ||
    (removal.isPending && removal.variables?.id === resume.id);

  const columns: Column<Resume>[] = [
    {
      key: "name",
      header: "Resume",
      primary: true,
      render: (r) => (
        <div className="resume-name">
          <span className="resume-name__title">{r.name}</span>
          <span className="muted">
            {r.file_name ?? "No file"}
            {r.file_type ? ` · ${r.file_type.toUpperCase()}` : ""}
          </span>
        </div>
      ),
    },
    { key: "type", header: "Type", render: (r) => r.resume_type || <span className="muted">—</span> },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <span className="badges">
          {r.is_default && <Badge tone="accent">Default</Badge>}
          {r.is_active ? <Badge tone="success">Active</Badge> : <Badge tone="neutral">Inactive</Badge>}
          {r.is_default && !r.is_active && (
            <Badge tone="warning" title="Applications only use an active default resume.">
              Not used
            </Badge>
          )}
        </span>
      ),
    },
    { key: "uploaded", header: "Uploaded", render: (r) => formatDate(r.created_at) },
    {
      key: "actions",
      header: "Actions",
      render: (r) => (
        <div className="row-actions">
          <Button size="sm" onClick={() => setViewing(r)} disabled={!r.file_name}>
            View text
          </Button>
          {!r.is_default && (
            <Button
              size="sm"
              disabled={busy(r)}
              onClick={() => update.mutate({ resume: r, changes: { is_default: true }, done: `${r.name} is now the default` })}
            >
              Make default
            </Button>
          )}
          <Button
            size="sm"
            disabled={busy(r)}
            onClick={() =>
              update.mutate({
                resume: r,
                changes: { is_active: !r.is_active },
                done: r.is_active ? `${r.name} deactivated` : `${r.name} activated`,
              })
            }
          >
            {r.is_active ? "Deactivate" : "Activate"}
          </Button>
          <Button size="sm" variant="danger" disabled={busy(r)} onClick={() => setDeleting(r)} aria-label={`Delete ${r.name}`}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const hasDefault = Boolean(resumes.data?.some((r) => r.is_default));

  return (
    <>
      <section className="panel" aria-labelledby="upload-title">
        <h2 id="upload-title" className="panel__title">
          Upload a resume
        </h2>
        {resumes.data ? (
          <ResumeUpload key={profile.id} profileId={profile.id} hasDefault={hasDefault} />
        ) : (
          !resumes.error && <Skeleton lines={2} label="Preparing upload" />
        )}
      </section>

      <section className="panel panel--flush" aria-labelledby="resume-list-title">
        <div className="panel__header panel__header--padded">
          <h2 id="resume-list-title" className="panel__title">
            Resumes for {profile.name}
          </h2>
        </div>
        <DataTable
          caption={`Resumes for ${profile.name}`}
          columns={columns}
          rows={resumes.data}
          rowKey={(r) => r.id}
          isLoading={resumes.isPending}
          error={resumes.error}
          onRetry={() => void resumes.refetch()}
          emptyTitle="No resumes yet"
          empty="Upload a resume above. Application drafts are written from its text."
        />
      </section>

      <ResumeTextDialog resume={viewing} onClose={() => setViewing(null)} />

      <ConfirmDialog
        open={deleting !== null}
        title={`Delete “${deleting?.name ?? ""}”?`}
        message="The stored file is deleted too. It cannot be downloaded or recovered from ApplyForge afterwards; keep your own copy."
        confirmLabel="Delete resume"
        tone="danger"
        onConfirm={async () => {
          if (!deleting) return;
          await removal.mutateAsync(deleting);
          setDeleting(null);
        }}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}

export function ResumesPage() {
  const { profile } = useActiveProfile();
  return (
    <>
      <PageHeader title="Resumes" description="Resumes used to prepare application drafts." />
      <RequireProfile>{profile && <ResumeList key={profile.id} profile={profile} />}</RequireProfile>
    </>
  );
}
