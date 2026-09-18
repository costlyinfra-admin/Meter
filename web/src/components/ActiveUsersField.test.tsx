import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ActiveUsersField } from "./ActiveUsersField";

const onSave = vi.fn();

beforeEach(() => {
  // A block, not an expression: `mockResolvedValue` returns the mock, and a hook
  // that returns a function has handed Vitest a teardown — which would then call
  // onSave after every test, rejecting into nobody's hands.
  onSave.mockReset().mockResolvedValue(undefined);
});

function show(value: number | null = null) {
  return render(<ActiveUsersField value={value} month="2026-05" onSave={onSave} />);
}

describe("the one number a person types", () => {
  it("offers a way in when nobody has told us yet", () => {
    show(null);
    // The blank state is the whole reason this control exists, so it has to say
    // what is missing rather than render nothing.
    expect(screen.getByText(/no active users recorded for may 2026/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /set active users/i })).toBeInTheDocument();
  });

  it("names the month it is about, because the page is showing a range", () => {
    show(540);
    expect(screen.getByText(/540 active users in May 2026/)).toBeInTheDocument();
  });

  it("saves what was typed", async () => {
    show(null);
    fireEvent.click(screen.getByRole("button", { name: /set active users/i }));
    fireEvent.change(screen.getByLabelText(/active users in May 2026/i), {
      target: { value: "540" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(540));
  });

  it("saves on Enter and abandons on Escape", async () => {
    show(12);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText(/active users in May 2026/i);

    fireEvent.change(input, { target: { value: "99" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(99));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.keyDown(screen.getByLabelText(/active users in May 2026/i), { key: "Escape" });
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("refuses anything that is not a whole number of people", async () => {
    show(null);
    fireEvent.click(screen.getByRole("button", { name: /set active users/i }));
    const input = screen.getByLabelText(/active users in May 2026/i);

    for (const bad of ["-4", "3.5", ""]) {
      fireEvent.change(input, { target: { value: bad } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/whole number/i);
      expect(onSave).not.toHaveBeenCalled();
    }
  });

  it("accepts zero, which is a real answer about a feature", async () => {
    show(40);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText(/active users in May 2026/i), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(0));
  });

  it("keeps what was typed when the save fails", async () => {
    onSave.mockImplementation(async () => {
      throw new Error("nope");
    });
    show(null);
    fireEvent.click(screen.getByRole("button", { name: /set active users/i }));
    fireEvent.change(screen.getByLabelText(/active users in May 2026/i), {
      target: { value: "77" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not save/i);
    expect(screen.getByLabelText(/active users in May 2026/i)).toHaveValue(77);
  });
});
