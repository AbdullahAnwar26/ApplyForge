import { useEffect } from "react";

export const APP_NAME = "ApplyForge";

/** Sets the browser tab title to "<page> · ApplyForge". */
export function useDocumentTitle(page: string | null | undefined): void {
  useEffect(() => {
    document.title = page ? `${page} · ${APP_NAME}` : APP_NAME;
  }, [page]);
}
