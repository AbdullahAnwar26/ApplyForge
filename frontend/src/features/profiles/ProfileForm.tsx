/**
 * Create or edit a profile. Fields and rules follow ProfileSerializer:
 * name and target_roles required; experience_years a number >= 0;
 * max_acceptable_experience_years a whole number >= 0; the rest optional text.
 */

import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { isApiError } from "../../api/errors";
import { createProfile, updateProfile } from "../../api/resources";
import type { Profile, ProfileInput } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Button } from "../../components/Button";
import { Field, FormErrors } from "../../components/Field";

const FIELDS = [
  "name",
  "target_roles",
  "professional_summary",
  "experience_info",
  "preferred_locations",
  "preferred_work_modes",
  "phone_country_code",
  "experience_years",
  "max_acceptable_experience_years",
] as const;
type FieldName = (typeof FIELDS)[number];
type Values = Record<FieldName, string>;
type Errors = Partial<Record<FieldName, string>>;

function initialValues(profile?: Profile): Values {
  return {
    name: profile?.name ?? "",
    target_roles: profile?.target_roles ?? "",
    professional_summary: profile?.professional_summary ?? "",
    experience_info: profile?.experience_info ?? "",
    preferred_locations: profile?.preferred_locations ?? "",
    preferred_work_modes: profile?.preferred_work_modes ?? "",
    phone_country_code: profile?.phone_country_code ?? "",
    experience_years: profile?.experience_years != null ? String(profile.experience_years) : "",
    max_acceptable_experience_years:
      profile?.max_acceptable_experience_years != null ? String(profile.max_acceptable_experience_years) : "",
  };
}

/** Client-side checks mirroring the serializer. Returns errors and the payload. */
export function validateProfile(values: Values, mode: "create" | "edit"): { errors: Errors; payload: ProfileInput } {
  const errors: Errors = {};
  const name = values.name.trim();
  const targetRoles = values.target_roles.trim();
  if (!name) errors.name = "Enter a name.";
  else if (name.length > 255) errors.name = "Use at most 255 characters.";
  if (!targetRoles) errors.target_roles = "Enter at least one target role.";
  if (values.preferred_work_modes.trim().length > 255) errors.preferred_work_modes = "Use at most 255 characters.";
  const countryCode = values.phone_country_code.trim().replace(/^\+/, "");
  if (countryCode && !/^\d{1,4}$/.test(countryCode)) errors.phone_country_code = "Enter the country calling code as digits, e.g. 91.";

  const payload: ProfileInput = {
    name,
    target_roles: targetRoles,
    professional_summary: values.professional_summary.trim(),
    experience_info: values.experience_info.trim(),
    preferred_locations: values.preferred_locations.trim(),
    preferred_work_modes: values.preferred_work_modes.trim(),
    phone_country_code: countryCode,
  };

  // Empty on create = use the backend default; on edit a value is required
  // (the backend fields are not nullable).
  const years = values.experience_years.trim();
  if (years) {
    const parsed = Number(years);
    if (!Number.isFinite(parsed) || parsed < 0) errors.experience_years = "Enter a number of years, 0 or more.";
    else payload.experience_years = parsed;
  } else if (mode === "edit") {
    errors.experience_years = "Enter your years of experience.";
  }

  const ceiling = values.max_acceptable_experience_years.trim();
  if (ceiling) {
    if (!/^\d+$/.test(ceiling)) errors.max_acceptable_experience_years = "Enter a whole number of years, 0 or more.";
    else payload.max_acceptable_experience_years = Number(ceiling);
  } else if (mode === "edit") {
    errors.max_acceptable_experience_years = "Enter the highest required experience to accept.";
  }

  return { errors, payload };
}

export function ProfileForm({
  profile,
  submitLabel,
  onSaved,
  onCancel,
}: {
  /** Edit this profile; omit to create a new one. */
  profile?: Profile;
  submitLabel: string;
  onSaved: (profile: Profile) => void;
  onCancel?: () => void;
}) {
  const api = useApi();
  const mode = profile ? "edit" : "create";
  const [values, setValues] = useState<Values>(() => initialValues(profile));
  const [clientErrors, setClientErrors] = useState<Errors>({});

  const mutation = useMutation({
    mutationFn: (payload: ProfileInput) =>
      profile ? updateProfile(api, profile.id, payload) : createProfile(api, payload),
    onSuccess: onSaved,
  });

  const serverError = mutation.error;
  function errorFor(field: FieldName): string | null {
    if (clientErrors[field]) return clientErrors[field]!;
    return isApiError(serverError) ? serverError.fieldMessage(field) : null;
  }

  function set(field: FieldName, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
    if (clientErrors[field]) setClientErrors((current) => ({ ...current, [field]: undefined }));
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mutation.isPending) return; // no duplicate submissions
    const { errors, payload } = validateProfile(values, mode);
    setClientErrors(errors);
    if (Object.keys(errors).length > 0) return;
    mutation.mutate(payload);
  }

  const text = (field: FieldName, label: string, options: { required?: boolean; hint?: string; multiline?: boolean } = {}) => (
    <Field label={label} hint={options.hint} error={errorFor(field)} required={options.required}>
      {(props) =>
        options.multiline ? (
          <textarea
            {...props}
            className="input input--multiline"
            rows={3}
            value={values[field]}
            onChange={(event) => set(field, event.target.value)}
          />
        ) : (
          <input
            {...props}
            className="input"
            value={values[field]}
            onChange={(event) => set(field, event.target.value)}
            required={options.required}
          />
        )
      }
    </Field>
  );

  return (
    <form className="form" onSubmit={onSubmit} noValidate aria-busy={mutation.isPending || undefined}>
      <fieldset className="form__fields" disabled={mutation.isPending}>
        <div className="form__grid">
          {text("name", "Name", { required: true })}
          {text("target_roles", "Target roles", {
            required: true,
            hint: "Comma-separated, e.g. Python Developer, Backend Engineer. Used as the default collector keywords.",
          })}
          <Field
            label="Years of experience"
            hint={mode === "create" ? "Leave empty to use the backend default (1)." : "Your actual experience; decimals allowed."}
            error={errorFor("experience_years")}
            required={mode === "edit"}
          >
            {(props) => (
              <input
                {...props}
                className="input"
                inputMode="decimal"
                value={values.experience_years}
                onChange={(event) => set("experience_years", event.target.value)}
              />
            )}
          </Field>
          <Field
            label="Highest experience requirement to accept"
            hint={
              mode === "create"
                ? "Jobs asking for more years are rejected in screening. Leave empty for the backend default (9)."
                : "Jobs asking for more years than this are rejected in screening."
            }
            error={errorFor("max_acceptable_experience_years")}
            required={mode === "edit"}
          >
            {(props) => (
              <input
                {...props}
                className="input"
                inputMode="numeric"
                value={values.max_acceptable_experience_years}
                onChange={(event) => set("max_acceptable_experience_years", event.target.value)}
              />
            )}
          </Field>
          {text("preferred_locations", "Preferred locations", { hint: "Comma-separated." })}
          {text("preferred_work_modes", "Preferred work modes", { hint: "e.g. Remote, Hybrid, On-site." })}
          {text("phone_country_code", "Phone country code", {
            hint: "e.g. 91. Completes WhatsApp numbers that recruiters write without a country code.",
          })}
        </div>
        {text("professional_summary", "Professional summary", {
          multiline: true,
          hint: "Used when drafting applications. Only include what is true.",
        })}
        {text("experience_info", "Experience details", { multiline: true })}
      </fieldset>

      <FormErrors error={serverError} shownFields={[...FIELDS]} />

      <div className="form__actions">
        {onCancel && (
          <Button onClick={onCancel} disabled={mutation.isPending}>
            Cancel
          </Button>
        )}
        <Button type="submit" variant="primary" loading={mutation.isPending}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
