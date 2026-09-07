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
