import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/errors";
import { StatusBadge } from "./Badge";
import { Button } from "./Button";
import { DataTable, type Column } from "./DataTable";
import { ConfirmDialog, Dialog } from "./Dialog";
import { Field, FormErrors } from "./Field";
import { Pagination } from "./Pagination";
import { EmptyState, ErrorState } from "./States";
import { ToastProvider, useToast } from "./Toast";

describe("Button", () => {
  it("is disabled and busy while loading", () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("type", "button");
  });
});

describe("StatusBadge", () => {
  it("uses the shared labels and tones", () => {
    render(
      <>
        <StatusBadge kind="screening" value="NEEDS_REVIEW" />
        <StatusBadge kind="screening" value={null} />
        <StatusBadge kind="application" value="FAILED" />
        <StatusBadge kind="collector" value="PARTIAL" />
        <StatusBadge kind="job" value="SOMETHING_NEW" />
      </>,
    );
    expect(screen.getByText("Needs review")).toHaveClass("badge--warning");
    expect(screen.getByText("Unscreened")).toHaveClass("badge--neutral");
    expect(screen.getByText("Generation failed")).toHaveClass("badge--danger");
    expect(screen.getByText("Partial")).toHaveClass("badge--warning");
    expect(screen.getByText("SOMETHING_NEW")).toHaveClass("badge--neutral"); // unknown values stay visible
  });
});

describe("EmptyState and ErrorState", () => {
  it("renders an empty state with an action", () => {
    render(<EmptyState title="No jobs yet" action={<button>Add</button>}>Run the collector.</EmptyState>);
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
  });

  it("shows the API error message and retries", async () => {
    const onRetry = vi.fn();
    render(<ErrorState error={new ApiError({ status: 409, detail: "Resume file is missing." })} onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Resume file is missing.");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("explains network errors and hides other error details", () => {
    const { rerender } = render(<ErrorState error={new ApiError({ status: 0 })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Is the Django server running?");
    rerender(<ErrorState error={new Error("internal stack detail")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("internal stack detail");
  });
});

interface Row {
  id: number;
  title: string;
  count: number;
}
const COLUMNS: Column<Row>[] = [
  { key: "title", header: "Title", render: (r) => r.title, primary: true },
  { key: "count", header: "Count", render: (r) => r.count, numeric: true },
];

describe("DataTable", () => {
  it("renders rows with labelled cells for the mobile layout", () => {
    render(<DataTable caption="Jobs" columns={COLUMNS} rows={[{ id: 1, title: "Backend Engineer", count: 3 }]} rowKey={(r) => r.id} />);
    const table = screen.getByRole("table", { name: "Jobs" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Title", "Count"]);
    const cells = within(table).getAllByRole("cell");
    expect(cells[0]).not.toHaveAttribute("data-label");
    expect(cells[1]).toHaveAttribute("data-label", "Count");
    expect(cells[1]).toHaveClass("is-numeric");
  });

  it("shows loading, empty and error states", () => {
    const { rerender } = render(<DataTable caption="Jobs" columns={COLUMNS} rows={undefined} rowKey={(r) => r.id} isLoading />);
    expect(screen.getByRole("status", { name: "Loading jobs" })).toBeInTheDocument();

    rerender(<DataTable caption="Jobs" columns={COLUMNS} rows={[]} rowKey={(r) => r.id} emptyTitle="No jobs match" />);
    expect(screen.getByText("No jobs match")).toBeInTheDocument();

    rerender(
      <DataTable caption="Jobs" columns={COLUMNS} rows={[]} rowKey={(r) => r.id} error={new ApiError({ status: 500 })} />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("shows the range and moves between pages", async () => {
    const onPageChange = vi.fn();
    const { rerender } = render(<Pagination page={1} pageSize={50} count={120} onPageChange={onPageChange} />);
    expect(screen.getByText("1–50 of 120")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(onPageChange).toHaveBeenCalledWith(2);

    rerender(<Pagination page={3} pageSize={50} count={120} onPageChange={onPageChange} />);
    expect(screen.getByText("101–120 of 120")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("renders nothing for an empty list", () => {
    const { container } = render(<Pagination page={1} pageSize={50} count={0} onPageChange={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

function DialogHarness({ onClose = () => {} }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <Dialog
        open={open}
        title="Edit skill"
        onClose={() => {
          onClose();
          setOpen(false);
        }}
        footer={<button>Save</button>}
      >
        <input aria-label="Name" />
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("moves focus inside, traps Tab, closes on Escape and restores focus", async () => {
    render(<DialogHarness />);
    const user = userEvent.setup();
    const opener = screen.getByRole("button", { name: "Open" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Edit skill" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const close = within(dialog).getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();

    await user.tab();
    expect(within(dialog).getByLabelText("Name")).toHaveFocus();
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "Save" })).toHaveFocus();
    await user.tab(); // wraps around
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(within(dialog).getByRole("button", { name: "Save" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});

describe("ConfirmDialog", () => {
  it("runs the action only on an explicit confirm", async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="Delete resume?" message="This cannot be undone." confirmLabel="Delete" tone="danger" onConfirm={onConfirm} onCancel={onCancel} />,
    );
    const user = userEvent.setup();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus(); // the safe choice
    expect(screen.getByRole("dialog")).toHaveAccessibleDescription("This cannot be undone.");

    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("cannot be dismissed while the action runs, and shows a failure", async () => {
    let fail: (error: Error) => void = () => {};
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Start collection?"
        message="Opens LinkedIn on the backend machine."
        confirmLabel="Start"
        onConfirm={() => new Promise<void>((_, reject) => (fail = reject))}
        onCancel={onCancel}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Start" }));
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(onCancel).not.toHaveBeenCalled();

    await act(async () => fail(new Error("Another collector run is in progress.")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Another collector run is in progress.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("Field and FormErrors", () => {
  it("wires the label, hint and error to the control", () => {
    render(
      <Field label="Name" hint="As shown to recruiters." error="This field is required." required>
        {(props) => <input {...props} />}
      </Field>,
    );
    const input = screen.getByLabelText(/Name/);
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("As shown to recruiters. This field is required.");
  });

  it("lists errors not shown next to a field", () => {
    const error = new ApiError({
      status: 400,
      fieldErrors: { name: ["Taken."], non_field_errors: ["Profile has no skills."], profile: ["Invalid pk."] },
    });
    render(<FormErrors error={error} shownFields={["name"]} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Profile has no skills.");
    expect(alert).toHaveTextContent("profile: Invalid pk.");
    expect(alert).not.toHaveTextContent("Taken.");
  });

  it("renders nothing when every error is shown by a field, or there is no error", () => {
    const { container, rerender } = render(
      <FormErrors error={new ApiError({ status: 400, fieldErrors: { name: ["Taken."] } })} shownFields={["name"]} />,
    );
    expect(container).toBeEmptyDOMElement();
    rerender(<FormErrors error={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

function ToastButton() {
  const { notify } = useToast();
  return <button onClick={() => notify("Saved", "success")}>Save</button>;
}

describe("Toast", () => {
  it("announces a message and can be dismissed", async () => {
    render(
      <ToastProvider>
        <ToastButton />
      </ToastProvider>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Save" }));
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(within(region).getByText("Saved")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(within(region).queryByText("Saved")).not.toBeInTheDocument();
  });

  it("disappears on its own", async () => {
    vi.useFakeTimers();
    try {
      render(
        <ToastProvider duration={1000}>
          <ToastButton />
        </ToastProvider>,
      );
      act(() => screen.getByRole("button", { name: "Save" }).click());
      expect(screen.getByText("Saved")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1000));
      expect(screen.queryByText("Saved")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
