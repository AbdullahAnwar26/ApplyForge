/**
 * Job Automation, three steps:
 *
 *   1. Find jobs   - search LinkedIn posts for your keywords (as many as you ask for).
 *   2. Pick jobs   - the ones matching your skills, with a recruiter email/phone, all
 *                    ticked; "Write applications" writes the email (+ WhatsApp message
 *                    when there is a number) for each.
 *   3. Apply       - one click opens each email in your Gmail (resume attached) or the
 *                    WhatsApp chat with the message; you press Send there.
 *
 * The step and the collection being picked from live in the URL (?step=pick&run=12).
 * Older links (?step=review / generate / send) land on the matching new step.
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { listApplicationsInStatuses } from "../../api/applications";
import { collectorKeys, isActive, listRuns } from "../../api/collector";
import { countJobs, jobKeys } from "../../api/jobs";
import type { Profile } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { PageHeader } from "../../components/PageHeader";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { APPLY_STATUSES, ApplyStep } from "./ApplyStep";
import { CollectForm, LiveRun } from "./CollectStep";
import { PickStep } from "./PickStep";

export type Step = "find" | "pick" | "apply";
const STEPS: { value: Step; label: string }[] = [
  { value: "find", label: "Find jobs" },
  { value: "pick", label: "Pick jobs" },
  { value: "apply", label: "Apply" },
];
const LEGACY: Record<string, Step> = { collect: "find", review: "pick", generate: "pick", send: "apply" };

export function readAutomationParams(search: URLSearchParams) {
  const raw = search.get("step") ?? "";
  const step = STEPS.find((s) => s.value === raw)?.value ?? LEGACY[raw] ?? "find";
  const rawRun = search.get("run");
  const run = rawRun && /^[1-9]\d{0,9}$/.test(rawRun) ? Number(rawRun) : null;
  return { step, run };
}

function StepCount({ value }: { value: number | undefined }) {
  if (!value) return null;
  return <span className="stepper__count">{value > 99 ? "99+" : value}</span>;
}

function Automation({ profile }: { profile: Profile }) {
  const api = useApi();
  const [search, setSearch] = useSearchParams();
  const { step, run } = readAutomationParams(search);
  const go = (changes: { step?: Step; run?: number | null }) => {
    const next = new URLSearchParams();
    const merged = { step, run, ...changes };
    if (merged.step !== "find") next.set("step", merged.step);
    if (merged.run && merged.step === "pick") next.set("run", String(merged.run));
    setSearch(next);
  };

  const latest = useQuery({
    queryKey: collectorKeys.latest,
    queryFn: ({ signal }) => listRuns(api, {}, 1, signal),
    select: (result) => result.items[0] ?? null,
  });
  const toPick = useQuery({
    queryKey: jobKeys.count({ status: "DISCOVERED" }),
    queryFn: ({ signal }) => countJobs(api, { status: "DISCOVERED" }, signal),
  });
  const toApply = useQuery({
    queryKey: ["applications", "list", "automation-counts", profile.id],
    queryFn: async ({ signal }) => (await listApplicationsInStatuses(api, profile.id, APPLY_STATUSES, 1, 1, signal)).count,
  });
  const latestRun = latest.data ?? null;
  const counts: Record<Step, number | undefined> = { find: undefined, pick: toPick.data, apply: toApply.data };

  let content: ReactNode;
  if (step === "find") {
    content = (
      <>
        <section className="panel" aria-labelledby="collect-title">
          <div>
            <h2 id="collect-title" className="panel__title">
              Find jobs
            </h2>
            <p className="panel__subtitle">
              Searches recruiters' LinkedIn posts for your keywords, opens each post fully and keeps the jobs with their contact
              details. Choose how many jobs you want.
            </p>
          </div>
          <CollectForm profile={profile} busy={isActive(latestRun)} onStarted={() => void latest.refetch()} />
        </section>
        {latestRun && (
          <section className="panel" aria-labelledby="latest-title">
            <h2 id="latest-title" className="panel__title">
              {isActive(latestRun) ? "Searching now" : "Latest search"}
            </h2>
            <LiveRun key={latestRun.id} runId={latestRun.id} onReview={(runId) => go({ step: "pick", run: runId })} />
          </section>
        )}
      </>
    );
  } else if (step === "pick") {
    content = <PickStep profile={profile} runId={run} onClearRun={() => go({ run: null })} onDone={() => go({ step: "apply" })} />;
  } else {
    content = <ApplyStep profile={profile} />;
  }

  return (
    <>
      <nav className="stepper stepper--3" aria-label="Job automation steps">
        <ol>
          {STEPS.map((s, index) => (
            <li key={s.value}>
              <button
                type="button"
                className={`stepper__step${step === s.value ? " stepper__step--active" : ""}`}
                aria-current={step === s.value ? "step" : undefined}
                onClick={() => go({ step: s.value, run: s.value === "pick" ? run : null })}
              >
                <span className="stepper__number">{index + 1}</span>
                <span className="stepper__label">{s.label}</span>
                <StepCount value={counts[s.value]} />
              </button>
            </li>
          ))}
        </ol>
      </nav>
      {content}
    </>
  );
}

export function AutomationPage() {
  const { profile } = useActiveProfile();
  return (
    <>
      <PageHeader
        title="Job Automation"
        description="Find jobs → pick the ones you want → apply in one click. Nothing is sent until you press Send yourself."
      />
      <RequireProfile>{profile && <Automation key={profile.id} profile={profile} />}</RequireProfile>
    </>
  );
}
