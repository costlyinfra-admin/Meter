import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { BuildCostActions } from "./BuildCostActions";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      listSeatSources: vi.fn(),
      manualBuildCost: vi.fn(),
      addManualBuildCost: vi.fn(),
      deleteManualBuildCost: vi.fn(),
    },
  };
});

describe("BuildCostActions — CSV help", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listSeatSources).mockResolvedValue([]);
  });

  it("documents the developer,github_handle,tool,amount,months format with an example", async () => {
    render(<BuildCostActions features={[]} onChanged={async () => {}} />);

    // Expand the CSV import card (awaiting the button also flushes the mount effect).
    fireEvent.click(await screen.findByRole("button", { name: /Import a CSV/ }));

    // The header format (now including months) is documented, github_handle explained.
    expect(screen.getByText("developer,github_handle,tool,amount,months")).toBeInTheDocument();
    expect(screen.getByText(/attribute PRs to features/i)).toBeInTheDocument();
    // The optional months column and its backfill behaviour are explained.
    expect(screen.getByText(/backfill history/i)).toBeInTheDocument();
    // The example row uses the new format with a months value.
    expect(screen.getByPlaceholderText(/John,John-ni,claude_code,50\.00,12/)).toBeInTheDocument();
  });
});

describe("BuildCostActions — adding a cost by hand", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listSeatSources).mockResolvedValue([]);
    vi.mocked(api.manualBuildCost).mockResolvedValue({ entries: [] });
  });

  const open = async () => {
    render(<BuildCostActions features={[]} onChanged={async () => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /Add a cost manually/ }));
    return screen.findByLabelText("Developer name");
  };

  it("says the figure is added, not a replacement", async () => {
    // The distinction that matters: every other path on this page replaces the
    // tool's month. Someone entering an invoice needs to know this one does not.
    const help = (await open()).closest(".method-panel")!.querySelector(".method-help")!;
    expect(help.textContent).toMatch(/added.*to the month rather than replacing it/i);
    expect(help.textContent).toMatch(/a later sync will not remove it/i);
  });

  it("sends what was typed, with the tool and a backfill span", async () => {
    vi.mocked(api.addManualBuildCost).mockResolvedValue({ total: 200 });
    await open();

    fireEvent.change(screen.getByLabelText("Developer name"), { target: { value: " Dana " } });
    fireEvent.change(screen.getByLabelText("GitHub handle"), { target: { value: "dpatel" } });
    fireEvent.change(screen.getByLabelText("Tool"), { target: { value: "claude_code" } });
    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "120.50" } });
    fireEvent.change(screen.getByLabelText("Months"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Add cost" }));

    await waitFor(() =>
      expect(api.addManualBuildCost).toHaveBeenCalledWith(
        expect.objectContaining({
          developer: "Dana",
          github_handle: "dpatel",
          tool: "claude_code",
          amount: 120.5,
          months: 3,
        }),
      ),
    );
  });

  it("will not submit without a developer or an amount", async () => {
    // A blank row would land in Unattributed as $0 and mean nothing.
    await open();
    expect(screen.getByRole("button", { name: "Add cost" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Developer name"), { target: { value: "Dana" } });
    expect(screen.getByRole("button", { name: "Add cost" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "50" } });
    expect(screen.getByRole("button", { name: "Add cost" })).not.toBeDisabled();
  });

  it("lists what was added so a typo can be removed", async () => {
    vi.mocked(api.manualBuildCost).mockResolvedValue({
      entries: [
        {
          id: "m1",
          developer: "Dana",
          handle: "dpatel",
          tool: "cursor",
          amount: 999,
          created_at: "2026-09-01T00:00:00Z",
        },
      ],
    });
    vi.mocked(api.deleteManualBuildCost).mockResolvedValue(undefined as never);
    await open();

    expect(await screen.findByText("Dana")).toBeInTheDocument();
    expect(screen.getByText("$999")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove Dana" }));
    await waitFor(() => expect(api.deleteManualBuildCost).toHaveBeenCalledWith("m1"));
  });
});
