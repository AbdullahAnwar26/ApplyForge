/**
 * The backend's OptionalPagination:
 *   - without `page`/`page_size`: a plain JSON array of all results;
 *   - with either: {count, next, previous, results}.
 * Both are normalised to ListResult.
 */

export interface PageResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface ListResult<T> {
  items: T[];
  /** Total number of matching rows (the array length when unpaginated). */
  count: number;
  paginated: boolean;
  hasNext: boolean;
  hasPrevious: boolean;
}

export interface PageParams {
  page?: number;
  pageSize?: number;
}

/** Backend defaults (REST_FRAMEWORK PAGE_SIZE and OptionalPagination.max_page_size). */
export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;

export function isPageResponse(value: unknown): value is PageResponse<unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const page = value as Record<string, unknown>;
  return typeof page.count === "number" && Array.isArray(page.results) && "next" in page && "previous" in page;
}

export function normalizeList<T>(body: unknown): ListResult<T> {
  if (Array.isArray(body)) {
    return { items: body as T[], count: body.length, paginated: false, hasNext: false, hasPrevious: false };
  }
  if (isPageResponse(body)) {
    return {
      items: body.results as T[],
      count: body.count,
      paginated: true,
      hasNext: body.next !== null,
      hasPrevious: body.previous !== null,
    };
  }
  throw new TypeError("Unexpected list response: expected an array or a page object.");
}

/** Query parameters for a page request. Empty when no paging is asked for (plain array). */
export function pageQuery(params: PageParams | undefined): Record<string, number> {
  if (!params || (params.page === undefined && params.pageSize === undefined)) return {};
  const query: Record<string, number> = {};
  if (params.page !== undefined) query.page = Math.max(1, Math.floor(params.page));
  if (params.pageSize !== undefined) {
    query.page_size = Math.min(MAX_PAGE_SIZE, Math.max(1, Math.floor(params.pageSize)));
  }
  return query;
}

export function pageCount(count: number, pageSize = DEFAULT_PAGE_SIZE): number {
  return Math.max(1, Math.ceil(count / Math.min(MAX_PAGE_SIZE, Math.max(1, pageSize))));
}
