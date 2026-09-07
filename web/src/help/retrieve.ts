/**
 * Retrieval for the support assistant.
 *
 * Separate from search.ts on purpose. The knowledge base's search box takes a
 * phrase someone typed to *find a page*, so a literal substring match is right
 * there. A chat question is a sentence — "how do I connect our GitHub org?" —
 * which contains no substring of any topic. So this scores per word instead,
 * weights rare words above common ones, and always returns something rather than
 * nothing: an assistant with no context can only say "I don't know".
 *
 * It runs over the whole book (a few dozen topics) on each question, which is
 * fast enough that there is no index to build and nothing to keep in sync — the
 * excerpts the model sees are always the docs the app is currently shipping.
 */
import { blockText, stripInline } from "./blocks";
import { ALL_TOPICS, type Category, type Topic } from "./content";

export interface Passage {
  /** `category/topic`, which is also its route under /help. */
  id: string;
  title: string;
  category: string;
  text: string;
}

/** Words too common in English — or in this handbook — to say anything. */
const STOP = new Set([
  "the",
  "and",
  "for",
  "you",
  "your",
  "our",
  "are",
  "was",
  "how",
  "what",
  "why",
  "who",
  "when",
  "where",
  "can",
  "did",
  "will",
  "would",
  "should",
  "could",
  "any",
  "all",
  "not",
  "but",
  "with",
  "from",
  "into",
  "that",
  "this",
  "these",
  "those",
  "there",
  "have",
  "has",
  "had",
  "get",
  "got",
  "use",
  "used",
  "using",
  "about",
  "than",
  "then",
  "them",
  "they",
  "its",
  "it's",
  "i'm",
  "were",
  "been",
  "being",
  "some",
  "each",
  "own",
  "out",
  "off",
  "one",
  "two",
  "see",
  "way",
  // The brand name, which appears in nearly every topic and so carries no
  // signal. Note this list is applied BEFORE stemming, so "metering" (the
  // concept) survives and stems to "meter" — the index's "meter" token
  // therefore means metering, not the product name.
  "meter",
  "please",
  "need",
  "want",
  "tell",
  "know",
  "make",
  "does",
  "doesn't",
]);

/** Short words that are real terms here and must survive the length filter. */
const KEEP_SHORT = new Set(["ai", "pr", "prs", "sdk", "api", "llm", "gpu", "aws", "id", "rls"]);

/** Crude suffix stripping, applied to the index and the question alike.
 *
 * Not linguistics — just enough that "secure" finds "security", "connect" finds
 * "connecting", and "attribute" finds "attribution". Over-stemming is harmless
 * here because both sides are stemmed the same way. */
function stem(word: string): string {
  for (const [suffix, replacement] of [
    ["ies", "y"],
    ["ions", "ion"],
    ["ing", ""],
    ["ity", ""],
    ["ion", ""],
    ["ed", ""],
    ["es", ""],
    ["s", ""],
  ] as const) {
    if (word.length > suffix.length + 3 && word.endsWith(suffix)) {
      return word.slice(0, -suffix.length) + replacement;
    }
  }
  return word;
}

function tokens(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((word) => word && !STOP.has(word) && (word.length > 2 || KEEP_SHORT.has(word)))
    .map(stem);
}

/** Every topic's searchable text, computed once — the book does not change. */
const DOCS = ALL_TOPICS.map(({ category, topic }: { category: Category; topic: Topic }) => {
  const body = topic.blocks.map(blockText).join(" ");
  return {
    category,
    topic,
    body,
    titleTokens: new Set(tokens(`${topic.title} ${category.title}`)),
    summaryTokens: new Set(tokens(topic.summary)),
    bodyTokens: new Set(tokens(body)),
  };
});

/** How many topics contain each word, for inverse-document-frequency weighting.
 *  "cost" appears everywhere and separates nothing; "webhook" appears twice and
 *  separates almost perfectly. */
const DF = new Map<string, number>();
for (const doc of DOCS) {
  for (const word of new Set([...doc.titleTokens, ...doc.summaryTokens, ...doc.bodyTokens])) {
    DF.set(word, (DF.get(word) ?? 0) + 1);
  }
}

function idf(word: string): number {
  return Math.log(1 + DOCS.length / (1 + (DF.get(word) ?? 0)));
}

/** Title and summary first, then as much body as the budget allows. */
function passageText(doc: (typeof DOCS)[number], limit: number): string {
  const head = `${doc.topic.title}. ${stripInline(doc.topic.summary)}`;
  return `${head} ${doc.body}`.slice(0, limit).trim();
}

export const MAX_PASSAGE_CHARS = 2400;

/**
 * The handbook excerpts most likely to answer `question`.
 *
 * Returns the highest-scoring topics, and — when a question matches nothing —
 * the opening topics of the book, so the assistant can still ground a reply on
 * what Meter is rather than answering from thin air.
 */
export function retrieve(question: string, count = 4): Passage[] {
  const words = [...new Set(tokens(question))];
  const scored = DOCS.map((doc) => {
    let score = 0;
    for (const word of words) {
      const weight = idf(word);
      if (doc.titleTokens.has(word)) score += 8 * weight;
      if (doc.summaryTokens.has(word)) score += 3 * weight;
      if (doc.bodyTokens.has(word)) score += 1 * weight;
    }
    return { doc, score };
  })
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, count);

  const chosen = scored.length ? scored.map((entry) => entry.doc) : DOCS.slice(0, 2);
  return chosen.map((doc) => ({
    id: `${doc.category.slug}/${doc.topic.slug}`,
    title: doc.topic.title,
    category: doc.category.title,
    text: passageText(doc, MAX_PASSAGE_CHARS),
  }));
}
