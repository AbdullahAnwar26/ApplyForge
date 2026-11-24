/**
 * The active profile's skills, grouped by category. Add and remove only (the
 * operations this screen needs); the backend enforces unique (name, profile).
 *
 * Duplicate prevention: a name already on the profile is refused here before any
 * request, ignoring case and surrounding spaces. The backend's uniqueness is
 * case-sensitive, but screening matches skills case-insensitively, so a second
 * "python" next to "Python" would add nothing.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { isApiError } from "../../api/errors";
import { createSkill, deleteSkill, listSkillCategories, listSkills, skillKeys } from "../../api/resources";
import type { Profile, Skill, SkillCategory, SkillCategoryOption } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { FormErrors } from "../../components/Field";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";

export const MAX_SKILL_NAME = 255;

export function normalizeSkillName(name: string): string {
  return name.trim().replace(/\s+/g, " ");
}

export function findDuplicate(skills: Skill[], name: string): Skill | undefined {
  const key = normalizeSkillName(name).toLocaleLowerCase();
  return skills.find((skill) => normalizeSkillName(skill.name).toLocaleLowerCase() === key);
}

function AddSkillForm({
  profileId,
  skills,
  categories,
}: {
  profileId: number;
  skills: Skill[];
  categories: SkillCategoryOption[];
}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const [name, setName] = useState("");
  const [category, setCategory] = useState<SkillCategory>(categories[0]?.value ?? "SKILL");
  const [problem, setProblem] = useState<string | null>(null);
  const nameId = useId();
  const categoryId = useId();
  const problemId = useId();

  const mutation = useMutation({
    mutationFn: (input: { name: string; category: SkillCategory }) => createSkill(api, { ...input, profile: profileId }),
    onSuccess: (skill) => {
      queryClient.setQueryData<Skill[]>(skillKeys.forProfile(profileId), (current = []) => [...current, skill]);
      void queryClient.invalidateQueries({ queryKey: skillKeys.forProfile(profileId) });
      setName("");
      notify(`Added ${skill.name}`, "success");
    },
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mutation.isPending) return;
    const clean = normalizeSkillName(name);
    if (!clean) return setProblem("Enter a skill name.");
    if (clean.length > MAX_SKILL_NAME) return setProblem(`Use at most ${MAX_SKILL_NAME} characters.`);
    const duplicate = findDuplicate(skills, clean);
    if (duplicate) return setProblem(`“${duplicate.name}” is already one of this profile's skills.`);
    setProblem(null);
    mutation.mutate({ name: clean, category });
  }

  // The backend reports a duplicate (e.g. added from another tab) as a non-field error.
  const serverError = mutation.error;
  const serverDuplicate =
    isApiError(serverError) && serverError.kind === "validation" && /unique/i.test(serverError.fieldMessage() ?? "");
  const nameError = problem ?? (serverDuplicate ? "This skill is already on the profile." : null) ??
    (isApiError(serverError) ? serverError.fieldMessage("name") : null);

  return (
    <form className="skill-form" onSubmit={onSubmit} noValidate>
      <div className="skill-form__row">
        <div className="field skill-form__name">
          <label className="field__label" htmlFor={nameId}>
            Skill or keyword
          </label>
          <input
            id={nameId}
            className="input"
            value={name}
            maxLength={MAX_SKILL_NAME + 50}
            onChange={(event) => {
              setName(event.target.value);
              if (problem) setProblem(null);
            }}
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? problemId : undefined}
            disabled={mutation.isPending}
            autoComplete="off"
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor={categoryId}>
            Category
          </label>
          <select
            id={categoryId}
            className="input"
            value={category}
            onChange={(event) => setCategory(event.target.value as SkillCategory)}
            disabled={mutation.isPending}
          >
            {categories.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" variant="primary" loading={mutation.isPending} className="skill-form__submit">
          Add skill
        </Button>
      </div>
      {nameError && (
        <p id={problemId} className="field__error" role="alert">
          {nameError}
        </p>
      )}
      {!serverDuplicate && <FormErrors error={serverError} shownFields={["name", "category", "profile"]} />}
    </form>
  );
}

export function SkillsPanel({ profile }: { profile: Profile }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const [pendingRemoval, setPendingRemoval] = useState<Skill | null>(null);

  const categories = useQuery({
    queryKey: skillKeys.categories,
    queryFn: ({ signal }) => listSkillCategories(api, signal),
    staleTime: Infinity, // fixed choices on the backend
  });
  const skills = useQuery({
    queryKey: skillKeys.forProfile(profile.id),
    queryFn: ({ signal }) => listSkills(api, profile.id, signal),
  });

  const removal = useMutation({
    mutationFn: (skill: Skill) => deleteSkill(api, skill.id),
    onSuccess: (_, skill) => {
      queryClient.setQueryData<Skill[]>(skillKeys.forProfile(profile.id), (current = []) =>
        current.filter((s) => s.id !== skill.id),
      );
      void queryClient.invalidateQueries({ queryKey: skillKeys.forProfile(profile.id) });
      notify(`Removed ${skill.name}`, "success");
    },
  });

  if (categories.error || skills.error) {
    return (
      <ErrorState
        title="Could not load skills"
        error={categories.error ?? skills.error}
        onRetry={() => {
          void categories.refetch();
          void skills.refetch();
        }}
      />
    );
  }
  if (!categories.data || !skills.data) return <Skeleton lines={4} label="Loading skills" />;

  const groups = categories.data
    .map((option) => ({ option, items: skills.data.filter((skill) => skill.category === option.value) }))
    .filter((group) => group.items.length > 0);
  const known = new Set(categories.data.map((c) => c.value));
  const uncategorised = skills.data.filter((skill) => !known.has(skill.category));

  return (
    <div className="skills">
      <AddSkillForm profileId={profile.id} skills={skills.data} categories={categories.data} />

      {skills.data.length === 0 ? (
        <EmptyState title="No skills yet">
          Jobs are screened by matching these skills and keywords against the job description. Add at least one.
        </EmptyState>
      ) : (
        <div className="skills__groups">
          {[...groups, ...(uncategorised.length ? [{ option: { value: "OTHER", label: "Other" }, items: uncategorised }] : [])].map(
            ({ option, items }) => (
              <section key={option.value} className="skills__group" aria-label={`${option.label} skills`}>
                <h3 className="skills__group-title">
                  {option.label} <span className="skills__count">{items.length}</span>
                </h3>
                <ul className="chips">
                  {items.map((skill) => (
                    <li key={skill.id} className="chip">
                      <span>{skill.name}</span>
                      <button
                        type="button"
                        className="chip__remove"
                        aria-label={`Remove ${skill.name}`}
                        onClick={() => setPendingRemoval(skill)}
                        disabled={removal.isPending && removal.variables?.id === skill.id}
                      >
                        ✕
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ),
          )}
        </div>
      )}

      <ConfirmDialog
        open={pendingRemoval !== null}
        title={`Remove “${pendingRemoval?.name ?? ""}”?`}
        message="Jobs screened from now on will not match this skill. Jobs already screened keep their result until they are screened again."
        confirmLabel="Remove skill"
        tone="danger"
        onConfirm={async () => {
          if (!pendingRemoval) return;
          await removal.mutateAsync(pendingRemoval);
          setPendingRemoval(null);
        }}
        onCancel={() => setPendingRemoval(null)}
      />
    </div>
  );
}
