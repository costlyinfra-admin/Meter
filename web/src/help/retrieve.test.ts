import { describe, expect, it } from "vitest";
import { retrieve } from "./retrieve";
import { findTopic } from "./content";

const ids = (question: string) => retrieve(question).map((p) => p.id);

describe("knowledge-base retrieval", () => {
  it("finds the right topic from a natural question, not just a keyword", () => {
    // The point of retrieve() over search(): none of this is a literal substring.
    expect(ids("why is some of our spend showing as unattributed?")).toContain(
      "concepts/unattributed",
    );
  });

  it("puts the topic a question is about first", () => {
    const [first] = retrieve("what is the difference between build cost and inference cost?");
    expect(first.id).toBe("concepts/build-vs-inference");
  });

  it("returns excerpts that carry the answer, not just the title", () => {
    const [first] = retrieve("what does confidence mean on a cost row?");
    expect(first.title).toBeTruthy();
    expect(first.text.length).toBeGreaterThan(200);
  });

  it("only ever cites topics that exist", () => {
    for (const passage of retrieve("how do I connect github and see per feature cost?")) {
      const [category, topic] = passage.id.split("/");
      expect(findTopic(category, topic)).toBeDefined();
    }
  });

  it("still grounds the assistant when a question matches nothing", () => {
    // Better to answer "here is what Meter is" than to answer from nothing.
    const passages = retrieve("qwertyuiop zxcvbnm");
    expect(passages.length).toBeGreaterThan(0);
  });

  it("is not fooled by words that appear in every topic", () => {
    // "cost" is everywhere, so it must not decide the ranking on its own.
    const [first] = retrieve("cost");
    const [specific] = retrieve("cost of a webhook alert");
    expect(first.id).not.toBe(specific.id);
  });

  it("matches a word to its other forms", () => {
    // The question says "connect"; the handbook topic is called "Connecting".
    expect(retrieve("how do I connect a provider?")[0].id).toBe("cost-sources/connecting");
  });

  it("finds the topic about asking for help", () => {
    expect(ids("how do I get help")).toContain("troubleshooting/getting-help");
  });

  it("keeps each excerpt within the size the API accepts", () => {
    for (const passage of retrieve("tell me everything about attribution and discovery")) {
      expect(passage.text.length).toBeLessThanOrEqual(2400);
    }
  });
});

describe("questions about coding agents", () => {
  // The assistant answers from whatever these four passages contain, so a topic
  // that exists but never gets retrieved may as well not exist. "Claude Code"
  // is the trap: it is also an AI coding tool whose seat spend Meter tracks, so
  // the build-cost topics compete for exactly the words a reader will type.
  it.each([
    "how do I connect Claude Code to Meter",
    "set up the MCP server",
    "can a coding agent change my data",
    "revoke an agent token",
    "what does an AI agent send outside Meter",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("trust/coding-agents");
  });

  it("leads with it, rather than with connecting a provider", () => {
    const [first] = retrieve("how do I connect Claude Code to Meter");
    expect(first.id).toBe("trust/coding-agents");
  });
});

describe("questions about FOCUS", () => {
  // "import" and "export" belong to the reconciliation topics, whose titles
  // carry them at eight times the weight — so a FOCUS question reached those
  // until this had a topic of its own.
  it.each([
    "can I import a FOCUS export",
    "does Meter support the FinOps billing format",
    "BilledCost or EffectiveCost",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("cost-sources/focus");
  });

  it("leads with it, rather than with reconciling an invoice", () => {
    expect(retrieve("can I import a FOCUS export")[0].id).toBe("cost-sources/focus");
  });
});

describe("questions about prompt caching", () => {
  // "cache" is everywhere in this product — the alert on a falling cache hit
  // rate, the Overview's token-type split, the repeated-request finding that
  // is literally about response caching. A reader asking why Meter did or did
  // not tell them to cache something has to land on the topic that explains
  // the arithmetic, not on one that merely uses the word.
  it.each([
    "should I turn on prompt caching",
    "why is Meter not recommending caching for this feature",
    "how is the prompt caching saving calculated",
    "does caching a prompt cost anything",
    "what is a cache write",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("optimize/prompt-caching");
  });

  it("leads with it, rather than with the cache-hit-rate alert", () => {
    expect(retrieve("should I turn on prompt caching")[0].id).toBe("optimize/prompt-caching");
  });
});

describe("questions about forecasting", () => {
  // "budget" pulls towards the Settings and budget-alert topics, and "forecast"
  // was only ever a word in the budget topic's summary. A reader asking what
  // next quarter will cost has to land on the page that answers it.
  it.each([
    "what will we spend next quarter",
    "forecast AI costs for the next three months",
    "which feature is driving our spend up",
    "how do I get warned before we go over budget",
    "why is there no range on this month's forecast",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("dashboards/forecast");
  });

  it("leads with it, rather than with setting a budget", () => {
    expect(retrieve("forecast AI costs for the next three months")[0].id).toBe(
      "dashboards/forecast",
    );
  });
});

describe("questions about testing a recommendation", () => {
  // "test" and "simulate" appear in the prompt-rewrite evaluation topic too,
  // which needs consent and spends tokens. A reader asking how to check a
  // caching or repeated-request finding first has to reach the free one.
  it.each([
    "how do I test a recommendation before changing anything",
    "can I simulate a response cache with a shorter freshness limit",
    "what does tested did not hold mean",
    "does running a test cost anything",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("optimize/testing-a-recommendation");
  });

  it("answers the cache-lifetime question from the caching topic, and points to the test", () => {
    // A question about which cache to use is first a question about caching,
    // so it lands on that topic — which has to say how to settle it.
    const [first] = retrieve("should I use the 1-hour prompt cache or the 5-minute one");
    expect(first.id).toBe("optimize/prompt-caching");
    const found = findTopic("optimize", "prompt-caching")!;
    expect(JSON.stringify(found.topic.blocks)).toContain("/help/optimize/testing-a-recommendation");
  });

  it("leads with it, rather than with testing a prompt rewrite", () => {
    expect(retrieve("how do I test a recommendation before changing anything")[0].id).toBe(
      "optimize/testing-a-recommendation",
    );
  });
});

describe("questions about live tests", () => {
  // "traffic" and "alert" pull towards the traces and alerts topics; a reader
  // asking how to try a cheaper model on real users has to reach this one.
  it.each([
    "how do I test a cheaper model on a share of live traffic",
    "what is a guardrail on a live test",
    "how do I send a quality score for an experiment",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("optimize/testing-a-recommendation");
  });
});

describe("questions about Meter running a model test", () => {
  // "captured", "consent" and "evaluation key" pull towards the prompt topics;
  // a reader asking whether Meter can test a cheaper model for them has to
  // reach the topic that explains what it costs and what it needs.
  it.each([
    "can Meter run the model test for me",
    "test a cheaper model on calls Meter captured",
    "what does a test run by Meter cost",
    "what happens if Meter restarts during a test",
  ])("reaches the topic from %j", (question) => {
    expect(ids(question)).toContain("optimize/testing-a-recommendation");
  });
});
