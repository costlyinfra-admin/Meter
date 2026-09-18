import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UsageImport } from "./UsageImport";
import { ApiError } from "../api";

const importUsage = vi.fn();

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { importUsage: (...args: unknown[]) => importUsage(...args) } };
});

beforeEach(() => {
  importUsage.mockReset().mockResolvedValue({ imported: 2, features: 2, periods: ["2026-05-01"] });
});

function open() {
  render(<UsageImport />);
  fireEvent.click(screen.getByRole("button", { name: /import a csv/i }));
}

describe("pasting in a month of adoption", () => {
  it("says why this has to be typed at all", () => {
    render(<UsageImport />);
    // Somebody looking at a blank column deserves to know it is not broken.
    expect(screen.getByText(/no connector that can read it/i)).toBeInTheDocument();
  });

  it("sends the pasted rows with the chosen month", async () => {
    open();
    fireEvent.change(screen.getByLabelText(/usage csv/i), {
      target: { value: "feature,active_users\nAI threat triage,540" },
    });
    fireEvent.change(screen.getByLabelText(/month/i), { target: { value: "2026-05" } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() =>
      expect(importUsage).toHaveBeenCalledWith(
        "feature,active_users\nAI threat triage,540",
        "2026-05",
      ),
    );
  });

  it("reports what landed", async () => {
    open();
    fireEvent.change(screen.getByLabelText(/usage csv/i), { target: { value: "a,b" } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    expect(await screen.findByText(/recorded active users for 2 features/i)).toBeInTheDocument();
  });

  it("passes the server's complaint straight through, row number and all", async () => {
    importUsage.mockImplementation(async () => {
      throw new ApiError(400, "Row 3: no feature called 'Reprot generator'.");
    });
    open();
    fireEvent.change(screen.getByLabelText(/usage csv/i), { target: { value: "a,b" } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    // Naming the row is the whole value of the error; a generic message would
    // leave someone hunting through a spreadsheet.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Row 3: no feature called 'Reprot generator'.",
    );
  });

  it("will not send an empty paste", () => {
    open();
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
  });
});
