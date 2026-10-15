/**
 * Knowledge base — the in-app handbook.
 *
 * Two layouts. The front page is a cover: what the handbook is, a search box, a
 * short path for a first read, and every chapter as a card. A topic is a page of
 * a book: contents down the left, the topic on the right with its reading time
 * and diagrams, and previous/next so it reads straight through.
 *
 * Content lives in help/content.ts as data, so topics can link into the product
 * itself rather than merely describing where to click. Search is client-side
 * over the whole book.
 */
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { readingMinutes } from "../help/blocks";
import { ChapterMark } from "../help/chapters";
import { chapterTone } from "../help/tones";
import { ALL_TOPICS, CATEGORIES, findTopic } from "../help/content";
import { Blocks } from "../help/render";
import { search } from "../help/search";

/** The first read: what Meter is, how to set it up, and what the numbers say. */
const START_HERE: [string, string][] = [
  ["getting-started", "what-meter-does"],
  ["getting-started", "setup"],
  ["getting-started", "first-dashboard"],
];

const TOTAL_MINUTES = ALL_TOPICS.reduce((sum, { topic }) => sum + readingMinutes(topic.blocks), 0);

function topicHref(categorySlug: string, topicSlug: string) {
  return `/help/${categorySlug}/${topicSlug}`;
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-4-4" strokeLinecap="round" />
    </svg>
  );
}

/** The cover illustration: bills in, cost per feature out. Decorative. */
function HeroArt() {
  return (
    <svg viewBox="0 0 360 260" className="kf-svg kb-hero-svg" aria-hidden>
      {[0, 1, 2].map((i) => (
        <g key={i} transform={`translate(${18 + i * 14} ${34 + i * 46}) rotate(${-6 + i * 5})`}>
          <rect width="118" height="64" rx="10" className="kf-card kf-lift" />
          <rect x="14" y="14" width="46" height="7" rx="3.5" className="kf-ink" />
          <rect x="14" y="30" width="80" height="5" rx="2.5" className="kf-empty" />
          <rect x="14" y="42" width="60" height="5" rx="2.5" className="kf-empty" />
          <text x="104" y="22" className="kf-td kf-sm" textAnchor="end">
            {["$18k", "$9k", "$4k"][i]}
          </text>
        </g>
      ))}
      <path d="M160 130 C 182 130, 184 130, 204 130" className="kf-beam" />
      <circle cx="182" cy="130" r="14" className="kf-run" />
      <path d="M177 130 h10 m-4 -4 l4 4 -4 4" className="kf-stroke-lime" />
      <rect x="212" y="40" width="136" height="180" rx="16" className="kf-card kf-lift" />
      <text x="226" y="64" className="kf-tl">
        PER FEATURE
      </text>
      {[
        [56, 88],
        [34, 52],
        [70, 30],
        [24, 64],
      ].map(([b, r], i) => (
        <g key={i} transform={`translate(226 ${80 + i * 34})`}>
          <rect width="64" height="6" rx="3" className="kf-empty" />
          <rect y="11" width={b} height="7" rx="3" className="kf-build" />
          <rect x={b + 3} y="11" width={r} height="7" rx="3" className="kf-run" />
        </g>
      ))}
      <circle cx="330" cy="28" r="5" className="kf-run" />
      <circle cx="196" cy="226" r="4" className="kf-build" />
      <circle cx="28" cy="236" r="3.5" className="kf-run" />
    </svg>
  );
}

function SearchResults({ query, onPick }: { query: string; onPick: () => void }) {
  const hits = useMemo(() => search(query), [query]);
  return (
    <>
      <p className="muted kb-result-count">
        {hits.length === 0
          ? "No topics match."
          : `${hits.length} ${hits.length === 1 ? "topic" : "topics"}`}
      </p>
      {hits.map(({ category, topic, snippet }) => (
        <Link
          key={`${category.slug}/${topic.slug}`}
          to={topicHref(category.slug, topic.slug)}
          className="kb-result"
          onClick={onPick}
        >
          <ChapterMark slug={category.slug} size="sm" />
          <span className="kb-result-text">
            <span className="kb-result-title">{topic.title}</span>
            <span className="muted kb-result-cat">{category.title}</span>
            <span className="muted kb-result-snippet">{snippet}</span>
          </span>
        </Link>
      ))}
    </>
  );
}

function Home() {
  const [query, setQuery] = useState("");
  const searching = query.trim().length >= 2;
  const topicCount = ALL_TOPICS.length;
  return (
    <div className="content kb kb-home">
      <section className="kb-hero">
        <div className="kb-hero-copy">
          <span className="kb-eyebrow">The Meter handbook</span>
          <h1>Knowledge base</h1>
          <p className="kb-hero-lede">
            What Meter does, how it works, and where every number comes from — in plain language,
            with pictures.
          </p>
          <label className="kb-hero-search">
            <SearchIcon />
            <input
              type="search"
              value={query}
              placeholder="Search — try “unattributed” or “prompt caching”"
              aria-label="Search the knowledge base"
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <p className="kb-hero-stats">
            <span>{CATEGORIES.length} chapters</span>
            <span>{topicCount} topics</span>
            <span>about {Math.round(TOTAL_MINUTES / 5) * 5} minutes, cover to cover</span>
          </p>
        </div>
        <div className="kb-hero-art">
          <HeroArt />
        </div>
      </section>

      {searching ? (
        <section className="kb-search-panel" aria-label="Search results">
          <SearchResults query={query} onPick={() => setQuery("")} />
        </section>
      ) : (
        <>
          <section className="kb-section">
            <div className="kb-section-head">
              <h2>Start here</h2>
              <p className="muted">Three short topics, and you will know how Meter thinks.</p>
            </div>
            <ol className="kb-start">
              {START_HERE.map(([c, t], i) => {
                const found = findTopic(c, t);
                if (!found) return null;
                return (
                  <li key={t}>
                    <Link to={topicHref(c, t)} className="kb-start-card">
                      <span className="kb-start-num">{i + 1}</span>
                      <span className="kb-start-title">{found.topic.title}</span>
                      <span className="muted kb-start-summary">{found.topic.summary}</span>
                      <span className="kb-start-time">
                        {readingMinutes(found.topic.blocks)} min read →
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ol>
          </section>

          <section className="kb-section">
            <div className="kb-section-head">
              <h2>Chapters</h2>
              <p className="muted">Read in order, or dip into the one you need.</p>
            </div>
            <div className="kb-chapters">
              {CATEGORIES.map((category, i) => {
                const first = category.topics[0];
                const shown = category.topics.slice(0, 4);
                const more = category.topics.length - shown.length;
                return (
                  <section
                    key={category.slug}
                    className={`kb-chapter tone-${chapterTone(category.slug)}`}
                  >
                    <div className="kb-chapter-head">
                      <ChapterMark slug={category.slug} size="lg" />
                      <span className="kb-chapter-num">
                        Chapter {String(i + 1).padStart(2, "0")}
                      </span>
                    </div>
                    <h3>{category.title}</h3>
                    <p className="muted">{category.blurb}</p>
                    <ul className="kb-chapter-topics">
                      {shown.map((topic) => (
                        <li key={topic.slug}>
                          <Link to={topicHref(category.slug, topic.slug)}>{topic.title}</Link>
                        </li>
                      ))}
                    </ul>
                    {first && (
                      <Link to={topicHref(category.slug, first.slug)} className="kb-chapter-go">
                        {more > 0 ? `Read all ${category.topics.length} topics` : "Start reading"} →
                      </Link>
                    )}
                  </section>
                );
              })}
            </div>
          </section>

          <section className="kb-ask">
            <div>
              <h2>Can&rsquo;t find it?</h2>
              <p>
                <strong>Ask Meter</strong>, bottom right, answers from your own data and this
                handbook, and shows where each answer came from.
              </p>
            </div>
            <Link to="/help/getting-started/ask-meter" className="kb-ask-link">
              How Ask Meter works →
            </Link>
          </section>
        </>
      )}
    </div>
  );
}

function TopicPage({ categorySlug, topicSlug }: { categorySlug: string; topicSlug: string }) {
  const [query, setQuery] = useState("");
  const current = findTopic(categorySlug, topicSlug);
  const index = current
    ? ALL_TOPICS.findIndex(
        ({ category, topic }) =>
          category.slug === current.category.slug && topic.slug === current.topic.slug,
      )
    : -1;
  const previous = index > 0 ? ALL_TOPICS[index - 1] : null;
  const next = index >= 0 && index < ALL_TOPICS.length - 1 ? ALL_TOPICS[index + 1] : null;
  const chapterIndex = current ? CATEGORIES.indexOf(current.category) : -1;
  const position = current ? current.category.topics.indexOf(current.topic) + 1 : 0;

  return (
    <div className="content kb">
      <div className="kb-body">
        <nav className="kb-nav" aria-label="Knowledge base contents">
          <Link to="/help" className="kb-nav-home">
            ← Handbook home
          </Link>
          <label className="kb-nav-search">
            <SearchIcon />
            <input
              type="search"
              value={query}
              placeholder="Search the handbook"
              aria-label="Search the knowledge base"
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          {query.trim().length >= 2 ? (
            <div className="kb-results">
              <SearchResults query={query} onPick={() => setQuery("")} />
            </div>
          ) : (
            CATEGORIES.map((category) => (
              <div key={category.slug} className="kb-cat">
                <span className="kb-cat-title">
                  <ChapterMark slug={category.slug} size="sm" />
                  {category.title}
                </span>
                <ul className="kb-topics">
                  {category.topics.map((topic) => {
                    const active = category.slug === categorySlug && topic.slug === topicSlug;
                    return (
                      <li key={topic.slug}>
                        <Link
                          to={topicHref(category.slug, topic.slug)}
                          className={active ? "kb-topic-link active" : "kb-topic-link"}
                          aria-current={active ? "page" : undefined}
                        >
                          {topic.title}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))
          )}
        </nav>

        <article className={`kb-article tone-${chapterTone(categorySlug)}`}>
          {current ? (
            <>
              <header className="kb-topic-head">
                <p className="kb-crumbs">
                  <Link to="/help">Knowledge base</Link>
                  <span aria-hidden> / </span>
                  <span>
                    Chapter {chapterIndex + 1} · {current.category.title}
                  </span>
                </p>
                <h1 className="kb-topic-title">
                  <ChapterMark slug={current.category.slug} size="lg" />
                  {current.topic.title}
                </h1>
                <p className="kb-summary">{current.topic.summary}</p>
                <p className="kb-meta">
                  <span>{readingMinutes(current.topic.blocks)} min read</span>
                  <span>
                    Topic {position} of {current.category.topics.length}
                  </span>
                  {current.topic.blocks.some((b) => b.kind === "figure") && (
                    <span>With a diagram</span>
                  )}
                </p>
              </header>
              <div className="kb-prose">
                <Blocks blocks={current.topic.blocks} />
              </div>

              <nav className="kb-pager" aria-label="Previous and next topics">
                {previous ? (
                  <Link
                    to={topicHref(previous.category.slug, previous.topic.slug)}
                    className="kb-pager-card"
                  >
                    <span className="kb-pager-label">Previous</span>
                    <span className="kb-pager-title">← {previous.topic.title}</span>
                  </Link>
                ) : (
                  <span />
                )}
                {next && (
                  <Link
                    to={topicHref(next.category.slug, next.topic.slug)}
                    className="kb-pager-card kb-pager-next"
                  >
                    <span className="kb-pager-label">
                      Next{next.category !== current.category && ` · ${next.category.title}`}
                    </span>
                    <span className="kb-pager-title">{next.topic.title} →</span>
                  </Link>
                )}
              </nav>
            </>
          ) : (
            <>
              <h1>That topic isn&rsquo;t here</h1>
              <p className="kb-summary">
                It may have moved. Search the contents, or go back to the{" "}
                <Link to="/help">handbook home</Link>.
              </p>
            </>
          )}
        </article>
      </div>
    </div>
  );
}

export function HelpPage() {
  const { category, topic } = useParams();
  return category && topic ? (
    <TopicPage key={`${category}/${topic}`} categorySlug={category} topicSlug={topic} />
  ) : (
    <Home />
  );
}
