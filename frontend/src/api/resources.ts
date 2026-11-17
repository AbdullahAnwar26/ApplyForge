/**
 * Endpoint functions for profiles, skills and resumes (backend/tracker/api).
 * Only operations the backend supports; no invented fields.
 */

import type { ApiClient } from "./client";
import type { Profile, ProfileInput, Resume, ResumeText, Skill, SkillCategory, SkillCategoryOption } from "./types";

// --- Profiles ------------------------------------------------------------------------

export function createProfile(api: ApiClient, input: ProfileInput): Promise<Profile> {
  return api.post<Profile>("profiles/", input);
}

export function updateProfile(api: ApiClient, id: number, input: Partial<ProfileInput>): Promise<Profile> {
  return api.patch<Profile>(`profiles/${id}/`, input);
}

// --- Skills ----------------------------------------------------------------------------

export const skillKeys = {
  categories: ["skill-categories"] as const,
  forProfile: (profileId: number) => ["skills", { profile: profileId }] as const,
};

export async function listSkillCategories(api: ApiClient, signal?: AbortSignal): Promise<SkillCategoryOption[]> {
  return api.get<SkillCategoryOption[]>("skill-categories/", { signal });
}

export async function listSkills(api: ApiClient, profileId: number, signal?: AbortSignal): Promise<Skill[]> {
  return (await api.list<Skill>("skills/", { query: { profile: profileId }, signal })).items;
}

export function createSkill(
  api: ApiClient,
  input: { name: string; category: SkillCategory; profile: number },
): Promise<Skill> {
  return api.post<Skill>("skills/", input);
}

export function deleteSkill(api: ApiClient, id: number): Promise<void> {
  return api.delete(`skills/${id}/`);
}

// --- Resumes ---------------------------------------------------------------------------

export const resumeKeys = {
  forProfile: (profileId: number) => ["resumes", { profile: profileId }] as const,
  text: (id: number) => ["resume-text", id] as const,
};

export async function listResumes(api: ApiClient, profileId: number, signal?: AbortSignal): Promise<Resume[]> {
  return (await api.list<Resume>("resumes/", { query: { profile: profileId }, signal })).items;
}

export interface ResumeUploadInput {
  profile: number;
  file: File;
  name?: string;
  resumeType?: string;
  isDefault?: boolean;
}

export function uploadResume(
  api: ApiClient,
  input: ResumeUploadInput,
  options: { onProgress?: (fraction: number) => void; signal?: AbortSignal } = {},
): Promise<Resume> {
  const form = new FormData();
  form.set("profile", String(input.profile));
  form.set("file", input.file, input.file.name);
  if (input.name?.trim()) form.set("name", input.name.trim());
  if (input.resumeType?.trim()) form.set("resume_type", input.resumeType.trim());
  form.set("is_default", input.isDefault ? "true" : "false");
  return api.uploadWithProgress<Resume>("POST", "resumes/", form, options);
}

export function updateResume(
  api: ApiClient,
  id: number,
  changes: Partial<Pick<Resume, "name" | "resume_type" | "is_active" | "is_default">>,
): Promise<Resume> {
  return api.patch<Resume>(`resumes/${id}/`, changes);
}

export function deleteResume(api: ApiClient, id: number): Promise<void> {
  return api.delete(`resumes/${id}/`);
}

export function getResumeText(api: ApiClient, id: number, signal?: AbortSignal): Promise<ResumeText> {
  return api.get<ResumeText>(`resumes/${id}/text/`, { signal });
}
