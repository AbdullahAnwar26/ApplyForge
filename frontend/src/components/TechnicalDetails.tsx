import { technicalDetail } from "../lib/diagnostics";

/** Collapsed backend diagnostic (URLs and OS error codes removed). Renders nothing for empty text. */
export function TechnicalDetails({ text }: { text: string | null | undefined }) {
  const detail = technicalDetail(text);
  if (!detail) return null;
  return (
    <details className="tech-details">
      <summary>Technical details</summary>
      <code>{detail}</code>
    </details>
  );
}
