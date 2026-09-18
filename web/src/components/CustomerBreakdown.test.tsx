/**
 * Customer economics: two cost categories on one screen, and the difference
 * between "nobody logged hours" and "nobody worked".
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CustomerBreakdown } from "./CustomerBreakdown";
import { ApiError, type CustomerSpend } from "../api";

const customerSpend = vi.fn();
const importHumanEffort = vi.fn();

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      customerSpend: (...a: unknown[]) => customerSpend(...a),
      importHumanEffort: (...a: unknown[]) => importHumanEffort(...a),
    },
  };
});

function customer(over: Partial<CustomerSpend["customers"][number]> = {}) {
  return {
    customer_id: "acme",
    amount: 600,
    pct: 80,
    requests: 20_000,
    cost_per_request: 0.03,
    prev_amount: 400,
    delta_pct: 50,
    months_active: 1,
    human_hours: null,
    human_cost: null,
    total_delivery_cost: 600,
    ...over,
  };
}

/** A tenant who has never uploaded effort — the shape every existing user is in. */
const METERED_ONLY: CustomerSpend = {
  start: "2026-05-01",
  end: "2026-05-01",
  months: 1,
  total: 750,
  customers: [
    customer(),
    customer({
      customer_id: "globex",
      amount: 150,
      pct: 20,
      cost_per_request: 0.005,
      prev_amount: null,
      delta_pct: null,
      total_delivery_cost: 150,
    }),
  ],
  trend: [{ period: "2026-05-01", amount: 750 }],
  inference_total: 5000,
  coverage_pct: 15,
  human_hours: null,
  human_cost: null,
  total_delivery_cost: 750,
  human_effort_present: false,
  human_effort_customer_count: 0,
  human_effort_ever: false,
  human_effort_trend: [],
};

/** ...and one who has. globex is the customer nobody logged hours against. */
const WITH_EFFORT: CustomerSpend = {
  ...METERED_ONLY,
  customers: [
    customer({ human_hours: 12, human_cost: 1320, total_delivery_cost: 1920 }),
    customer({
      customer_id: "globex",
      amount: 150,
      pct: 20,
      cost_per_request: 0.005,
      prev_amount: null,
      delta_pct: null,
      total_delivery_cost: 150,
    }),
    customer({
      customer_id: "effort-only-co",
      amount: null,
      pct: 0,
      requests: null,
      cost_per_request: null,
      prev_amount: null,
      delta_pct: null,
      human_hours: 40,
      human_cost: 5600,
      total_delivery_cost: 5600,
    }),
  ],
  human_hours: 52,
  human_cost: 6920,
  total_delivery_cost: 7670,
  human_effort_present: true,
  human_effort_customer_count: 2,
  human_effort_ever: true,
  human_effort_trend: [{ period: "2026-05-01", amount: 6920 }],
};

/** Selecting a file, the way the browser hands it over. */
function chooseFile(text = "date,person,customer_id,hours,activity_type,loaded_hourly_rate\n") {
  const file = new File([text], "effort.csv", { type: "text/csv" });
  // jsdom's File has no .text(); make it deterministic (as CsvBillImport.test does).
  Object.defineProperty(file, "text", { value: () => Promise.resolve(text) });
  fireEvent.change(screen.getByLabelText(/effort csv file/i), { target: { files: [file] } });
}

function show() {
  return render(
    <MemoryRouter>
      <CustomerBreakdown range={{ kind: "last_month" }} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  customerSpend.mockReset().mockResolvedValue(METERED_ONLY);
  importHumanEffort.mockReset().mockResolvedValue({
    imported: 2,
    batch_id: "b1",
    customers: 2,
    hours: 5.5,
    cost: 635,
    first_date: "2026-05-15",
    last_date: "2026-05-16",
  });
});

describe("states that must keep working", () => {
  it("loads, then renders", async () => {
    customerSpend.mockImplementation(() => new Promise(() => {}));
    show();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("says so when the request fails", async () => {
    customerSpend.mockImplementation(async () => {
      throw new ApiError(500, "boom");
    });
    show();
    expect(await screen.findByText(/couldn't load customer spend/i)).toBeInTheDocument();
  });

  it("still explains the SDK when nothing is tagged", async () => {
    customerSpend.mockResolvedValue({ ...METERED_ONLY, customers: [], total: 0 });
    show();
    expect(await screen.findByText("No customer-attributed spend yet")).toBeInTheDocument();
  });

  it("renders a tenant who has only ever had metered cost", async () => {
    show();
    expect(await screen.findByText("Customer economics")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("acme")).toBeInTheDocument();
    // The columns that were here before still say what they said.
    expect(within(table).getByText("$0.03")).toBeInTheDocument();
    expect(within(table).getByText(/▲ 50%/)).toBeInTheDocument();
    expect(screen.getByText(/15% of the \$5,000 inference bill/)).toBeInTheDocument();
  });
});

describe("the two cost categories", () => {
  it("summarises AI, hours, human cost and delivery cost", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    const summary = await screen.findByRole("region", { name: /customer economics summary/i });

    expect(within(summary).getByText("Metered AI cost")).toBeInTheDocument();
    expect(within(summary).getByText("$750")).toBeInTheDocument();
    expect(within(summary).getByText("52")).toBeInTheDocument();
    expect(within(summary).getByText("$6,920")).toBeInTheDocument();
    expect(within(summary).getByText("$7,670")).toBeInTheDocument();
  });

  it("never presents delivery cost as the AI bill", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    await screen.findByText("Customer economics");
    expect(screen.getByText(/not the full bill/i)).toBeInTheDocument();
    // The authoritative bill is still named, and is larger.
    expect(screen.getByText(/\$5,000 inference bill/)).toBeInTheDocument();
  });

  it("renders Not provided — never a zero — where effort is missing", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    await screen.findByText("Customer economics");

    const table = screen.getByRole("table");
    const globex = within(table).getByText("globex").closest("tr")!;
    expect(within(globex).getAllByText("Not provided")).toHaveLength(2); // hours + cost
    expect(within(globex).queryByText("$0")).not.toBeInTheDocument();
    expect(within(globex).queryByText("0")).not.toBeInTheDocument();
  });

  it("shows Not provided in the summary when no effort exists at all", async () => {
    show();
    const summary = await screen.findByRole("region", { name: /customer economics summary/i });
    expect(within(summary).getAllByText("Not provided")).toHaveLength(2); // hours + cost
    expect(within(summary).queryByText("0 hrs")).not.toBeInTheDocument();
  });

  it("lists a customer that has effort and no metered calls", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    await screen.findByText("Customer economics");

    const table = screen.getByRole("table");
    const row = within(table).getByText("effort-only-co").closest("tr")!;
    const cells = [...row.querySelectorAll("td")].map((td) => td.textContent);
    // AI cost unknown (not zero — nobody instrumented those calls), requests
    // absent, and the two human columns carrying the whole delivery cost.
    expect(cells[1]).toBe("Not provided");
    expect(cells[2]).toBe("—");
    expect(cells[4]).toBe("40");
    expect(cells[5]).toBe("$5,600");
    expect(cells[6]).toBe("$5,600");
  });

  it("counts how many customers have effort, against the customers shown", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    expect(
      await screen.findByText(/Human-effort data was provided for 2 of 3 customers/),
    ).toBeInTheDocument();
  });

  it("tells a period with no effort apart from a tenant that has never uploaded", async () => {
    customerSpend.mockResolvedValue({ ...METERED_ONLY, human_effort_ever: true });
    show();
    expect(await screen.findByText(/though earlier periods have it/i)).toBeInTheDocument();
  });
});

describe("top customers", () => {
  it("ranks by delivery cost when there is effort, and by AI cost when not", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    const { unmount } = show();
    await screen.findByText("Customer economics");
    expect(screen.getByLabelText(/rank customers by/i)).toHaveValue("delivery");
    unmount();

    customerSpend.mockResolvedValue(METERED_ONLY);
    show();
    await screen.findByText("Customer economics");
    expect(screen.getByLabelText(/rank customers by/i)).toHaveValue("ai");
  });

  it("changes the ranking when the metric changes", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    await screen.findByText("Customer economics");
    const bars = () =>
      [...document.querySelectorAll(".provider-bar-name")].map((n) => n.textContent ?? "");

    // By delivery cost the effort-only customer leads; by AI cost it is absent.
    expect(bars()[0]).toContain("effort-only-co");
    fireEvent.change(screen.getByLabelText(/rank customers by/i), { target: { value: "ai" } });
    expect(bars()[0]).toContain("acme");
    expect(bars().join(" ")).not.toContain("effort-only-co");

    // Hours are rendered as hours, not as dollars.
    fireEvent.change(screen.getByLabelText(/rank customers by/i), {
      target: { value: "human_hours" },
    });
    expect(document.querySelector(".provider-bar-amt")?.textContent).toMatch(/40 hrs/);
  });
});

describe("privacy", () => {
  it("keeps customer identifiers masked for session replay", async () => {
    customerSpend.mockResolvedValue(WITH_EFFORT);
    show();
    await screen.findByText("Customer economics");

    const table = screen.getByRole("table");
    for (const id of ["acme", "globex", "effort-only-co"]) {
      const cell = within(table).getByText(id).closest("td")!;
      expect(cell).toHaveAttribute("data-dd-privacy", "mask");
    }
    // ...and the bar labels, which are the same identifiers.
    const bars = document.querySelector(".provider-bars")!.closest("[data-dd-privacy]");
    expect(bars).toHaveAttribute("data-dd-privacy", "mask");
  });
});

describe("importing effort", () => {
  it("imports a chosen file and refreshes the breakdown", async () => {
    show();
    await screen.findByText("Customer economics");
    fireEvent.click(screen.getByRole("button", { name: "Add human effort" }));

    chooseFile();
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() => expect(importHumanEffort).toHaveBeenCalled());
    expect(await screen.findByRole("status")).toHaveTextContent(/imported 2 rows/i);
    // The screen re-pulls, so the new numbers are the ones on screen.
    await waitFor(() => expect(customerSpend).toHaveBeenCalledTimes(2));
  });

  it("shows the server's row-level complaint verbatim", async () => {
    importHumanEffort.mockImplementation(async () => {
      throw new ApiError(400, "Row 4: hours must be greater than 0, got '-2'.");
    });
    show();
    await screen.findByText("Customer economics");
    fireEvent.click(screen.getByRole("button", { name: "Add human effort" }));

    chooseFile();
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    // Naming the row is the whole value of the error.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Row 4: hours must be greater than 0, got '-2'.",
    );
  });

  it("explains the columns and offers an example", async () => {
    show();
    await screen.findByText("Customer economics");
    fireEvent.click(screen.getByRole("button", { name: "Add human effort" }));

    expect(screen.getAllByText(/loaded_hourly_rate/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /copy example/i })).toBeInTheDocument();
    // Nothing is stored by choosing a file.
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
  });
});
