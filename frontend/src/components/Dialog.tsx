/**
 * Modal dialog: rendered in a portal, focus moves inside and is trapped (Tab /
 * Shift+Tab), Escape and the close button dismiss it, focus returns to the
 * element that opened it, and the page behind is inert.
 */

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Button } from "./Button";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Dialog({
  open,
  title,
  description,
  onClose,
  children,
  footer,
  dismissible = true,
  wide = false,
}: {
  open: boolean;
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children?: ReactNode;
  footer?: ReactNode;
  /** False while an action is in progress: Escape and the close button do nothing. */
  dismissible?: boolean;
  /** Wider panel for forms. */
  wide?: boolean;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const openerRef = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement;
    const root = document.getElementById("root");
    root?.setAttribute("inert", "");
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>("[data-autofocus]") ?? panel?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel)?.focus();
    return () => {
      root?.removeAttribute("inert");
      const opener = openerRef.current;
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, [open]);

  if (!open) return null;

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      if (dismissible) onClose();
      return;
    }
    if (event.key !== "Tab" || !panelRef.current) return;
    const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const firstItem = items[0]!;
    const lastItem = items[items.length - 1]!;
    if (event.shiftKey && document.activeElement === firstItem) {
      event.preventDefault();
      lastItem.focus();
    } else if (!event.shiftKey && document.activeElement === lastItem) {
      event.preventDefault();
      firstItem.focus();
    }
  }

  return createPortal(
    <div className="dialog-backdrop">
      <div
        ref={panelRef}
        className={wide ? "dialog dialog--wide" : "dialog"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="dialog__header">
          <h2 id={titleId} className="dialog__title">
            {title}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={!dismissible} aria-label="Close">
            ✕
          </Button>
        </div>
        {description && (
          <div id={descriptionId} className="dialog__description">
            {description}
          </div>
        )}
        {children && <div className="dialog__body">{children}</div>}
        {footer && <div className="dialog__footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

/**
 * Asks before an action. The action runs only on an explicit click of the
 * confirm button; while it runs the dialog cannot be dismissed. If it throws,
 * the error is shown and the dialog stays open.
 */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  cancelLabel = "Cancel",
  tone = "primary",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "primary" | "danger";
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function confirm() {
    setPending(true);
    setFailure(null);
    try {
      await onConfirm();
    } catch (error) {
      setFailure(error instanceof Error && error.message ? error.message : "The action failed.");
    } finally {
      setPending(false);
    }
  }

  function cancel() {
    setFailure(null);
    onCancel();
  }

  return (
    <Dialog
      open={open}
      title={title}
      description={message}
      onClose={cancel}
      dismissible={!pending}
      footer={
        <>
          <Button onClick={cancel} disabled={pending} data-autofocus>
            {cancelLabel}
          </Button>
          <Button variant={tone} onClick={confirm} loading={pending}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {failure && (
        <p className="notice notice--error" role="alert">
          {failure}
        </p>
      )}
    </Dialog>
  );
}
