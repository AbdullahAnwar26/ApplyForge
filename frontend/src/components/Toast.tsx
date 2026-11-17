/**
 * Short, non-blocking confirmations ("Saved", "Active profile changed") in a
 * polite live region. Errors that need attention belong on the page, not here.
 */

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import type { Tone } from "../lib/labels";

export interface ToastItem {
  id: number;
  message: string;
  tone: Extract<Tone, "neutral" | "success" | "warning" | "danger">;
}

interface ToastContextValue {
  notify(message: string, tone?: ToastItem["tone"]): void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export const TOAST_DURATION_MS = 5000;

export function ToastProvider({ children, duration = TOAST_DURATION_MS }: { children: ReactNode; duration?: number }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((t) => t.id !== id)), []);

  const notify = useCallback(
    (message: string, tone: ToastItem["tone"] = "neutral") => {
      const id = nextId.current++;
      setToasts((current) => [...current.slice(-2), { id, message, tone }]); // at most 3
      window.setTimeout(() => dismiss(id), duration);
    },
    [dismiss, duration],
  );

  const value = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast--${toast.tone}`}>
            <span>{toast.message}</span>
            <button type="button" className="toast__close" onClick={() => dismiss(toast.id)} aria-label="Dismiss">
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast must be used inside <ToastProvider>.");
  return value;
}
