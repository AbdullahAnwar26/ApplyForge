/**
 * A plain, accessible table. Below 768px each row renders as a card, using each
 * column's header as the cell label (CSS, via data-label).
 */

import type { ReactNode } from "react";

import { EmptyState, ErrorState, Skeleton } from "./States";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  /** Right-align numbers. */
  numeric?: boolean;
  /** Hide the label in the mobile card layout (e.g. a title column). */
  primary?: boolean;
}

export function DataTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  isLoading = false,
  error,
  onRetry,
  emptyTitle = "Nothing here yet",
  empty,
}: {
  caption: string;
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string | number;
  isLoading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  emptyTitle?: string;
  empty?: ReactNode;
}) {
  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (isLoading || !rows) return <Skeleton lines={5} label={`Loading ${caption.toLowerCase()}`} />;
  if (rows.length === 0) return <EmptyState title={emptyTitle}>{empty}</EmptyState>;

  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} scope="col" className={column.numeric ? "is-numeric" : undefined}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  data-label={column.primary ? undefined : column.header}
                  className={[column.numeric ? "is-numeric" : "", column.primary ? "is-primary" : ""].join(" ").trim() || undefined}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
