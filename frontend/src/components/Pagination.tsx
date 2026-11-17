import { pageCount } from "../api/pagination";
import { Button } from "./Button";

/** Page navigation for DRF-paginated lists (`count` from the page response). */
export function Pagination({
  page,
  pageSize,
  count,
  onPageChange,
  disabled = false,
}: {
  page: number;
  pageSize: number;
  count: number;
  onPageChange: (page: number) => void;
  disabled?: boolean;
}) {
  const pages = pageCount(count, pageSize);
  if (count === 0) return null;
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(count, page * pageSize);

  return (
    <nav className="pagination" aria-label="Pagination">
      <p className="pagination__summary" aria-live="polite">
        {first}–{last} of {count}
      </p>
      <div className="pagination__controls">
        <Button size="sm" onClick={() => onPageChange(page - 1)} disabled={disabled || page <= 1}>
          Previous
        </Button>
        <span className="pagination__page">
          Page {page} of {pages}
        </span>
        <Button size="sm" onClick={() => onPageChange(page + 1)} disabled={disabled || page >= pages}>
          Next
        </Button>
      </div>
    </nav>
  );
}
