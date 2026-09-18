import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  DISCOVERED_KEY,
  DISMISSED_KEY,
  INVITED_KEY,
  hasDiscoveredChat,
  hasSeenInvitation,
  markChatDiscovered,
  markDismissed,
  markInvitationSeen,
  overviewContext,
  publishOverviewContext,
  resetAskMeter,
  trackOnce,
  wasDismissed,
} from "./askMeter";
import { track } from "./observability/datadog";

vi.mock("./observability/datadog", () => ({ track: vi.fn() }));

beforeEach(() => {
  localStorage.clear();
  resetAskMeter();
  vi.mocked(track).mockClear();
});

describe("what the bubble remembers", () => {
  it("starts with a clean slate", () => {
    expect(hasDiscoveredChat()).toBe(false);
    expect(hasSeenInvitation()).toBe(false);
    expect(wasDismissed()).toBe(false);
  });

  it("records each state under its own key", () => {
    markChatDiscovered();
    markInvitationSeen();
    markDismissed();
    expect(localStorage.getItem(DISCOVERED_KEY)).toBe("1");
    expect(localStorage.getItem(INVITED_KEY)).toBe("1");
    expect(localStorage.getItem(DISMISSED_KEY)).toBe("1");
  });

  it("forgets everything on reset, so the flow can be replayed", () => {
    markChatDiscovered();
    markInvitationSeen();
    markDismissed();
    resetAskMeter();
    expect(hasDiscoveredChat()).toBe(false);
    expect(hasSeenInvitation()).toBe(false);
    expect(wasDismissed()).toBe(false);
  });

  it("treats blocked storage as 'not seen' rather than throwing into a render", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("The operation is insecure.");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("The operation is insecure.");
    });
    expect(hasDiscoveredChat()).toBe(false);
    expect(() => markChatDiscovered()).not.toThrow();
    getItem.mockRestore();
    setItem.mockRestore();
  });
});

describe("counting", () => {
  it("counts a one-shot event once, however many times it is reported", () => {
    trackOnce("ask_meter_impression");
    trackOnce("ask_meter_impression");
    trackOnce("ask_meter_impression");
    expect(track).toHaveBeenCalledTimes(1);
  });

  it("counts again after a reset, so a replayed flow is measurable", () => {
    trackOnce("ask_meter_impression");
    resetAskMeter();
    trackOnce("ask_meter_impression");
    expect(track).toHaveBeenCalledTimes(2);
  });
});

describe("the Overview's context", () => {
  it("is readable by a bubble that mounted before the page's data landed", () => {
    publishOverviewContext([
      { label: "Explain the cost spike", detail: "…", topic: "the cost spike", question: "?" },
    ]);
    expect(overviewContext()).toHaveLength(1);
  });
});
