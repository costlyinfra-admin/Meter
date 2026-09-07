/**
 * Knowledge base — the in-app handbook.
 *
 * Laid out like a book: categories in reading order down the left, one topic at a
 * time on the right, with previous/next so it can be read straight through as
 * well as dipped into. Search is client-side over the whole book.
 *
 * Content lives in help/content.ts as data, so topics can link into the product
 * itself rather than merely describing where to click.
 */
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Blocks } from "../help/render";
import { ALL_TOPICS, CATEGORIES, findTopic } from "../help/content";
import { search } from "../help/search";

export function HelpPage() {
  const { category: categorySlug, topic: topicSlug } = useParams();
  const [query, setQuery] = useState("");

  const hits = useMemo(() => search(query), [query]);
  const current = categorySlug && topicSlug ? findTopic(categorySlug, topicSlug) : undefined;
  const index = current
    ? ALL_TOPICS.findIndex(
        ({ category, topic }) =>
          category.slug === current.category.slug && topic.slug === current.topic.slug,
      )
    : -1;
  const previous = index > 0 ? ALL_TOPICS[index - 1] : null;
  const next = index >= 0 && index < ALL_TOPICS.length - 1 ? ALL_TOPICS[index + 1] : null;

  return (
    <div className="content kb">
      <div className="dash-head">
        <h1>Knowledge base</h1>
      </div>

      <div className="kb-body">
        <nav className="kb-nav" aria-label="Knowledge base contents">
          <input
            className="kb-search"
            type="search"
            value={query}
            placeholder="Search the knowledge base"
            aria-label="Search the knowledge base"
            onChange={(e) => setQuery(e.target.value)}
          />
          {query.trim().length >= 2 ? (
            <div className="kb-results">
              <p className="muted kb-result-count">
                {hits.length === 0
                  ? "No topics match."
                  : `${hits.length} ${hits.length === 1 ? "topic" : "topics"}`}
              </p>
              {hits.map(({ category, topic, snippet }) => (
                <Link
                  key={`${category.slug}/${topic.slug}`}
                  to={`/help/${category.slug}/${topic.slug}`}
                  className="kb-result"
                  onClick={() => setQuery("")}
                >
                  <span className="kb-result-title">{topic.title}</span>
                  <span className="muted kb-result-cat">{category.title}</span>
                  <span className="muted kb-result-snippet">{snippet}</span>
                </Link>
              ))}
            </div>
          ) : (
            CATEGORIES.map((category) => (
              <div key={category.slug} className="kb-cat">
                <span className="kb-cat-title">{category.title}</span>
                <ul className="kb-topics">
                  {category.topics.map((topic) => {
                    const active = category.slug === categorySlug && topic.slug === topicSlug;
                    return (
                      <li key={topic.slug}>
                        <Link
                          to={`/help/${category.slug}/${topic.slug}`}
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

        <article className="kb-article">
          {current ? (
            <>
              <p className="kb-crumb muted">{current.category.title}</p>
              <h2>{current.topic.title}</h2>
              <p className="kb-summary">{current.topic.summary}</p>
              <Blocks blocks={current.topic.blocks} />

              <div className="kb-pager">
                {previous ? (
                  <Link to={`/help/${previous.category.slug}/${previous.topic.slug}`}>
                    ← {previous.topic.title}
                  </Link>
                ) : (
                  <span />
                )}
                {next && (
                  <Link to={`/help/${next.category.slug}/${next.topic.slug}`}>
                    {next.topic.title} →
                  </Link>
                )}
              </div>
            </>
          ) : (
            <>
              <h2>Everything about Meter, in one place</h2>
              <p className="kb-summary">
                Start at the beginning, or search for what you need. Every topic describes what
                Meter actually does — including where each number comes from.
              </p>
              <div className="kb-toc">
                {CATEGORIES.map((category) => (
                  <section key={category.slug} className="kb-toc-cat">
                    <h3>{category.title}</h3>
                    <p className="muted">{category.blurb}</p>
                    <ul className="kb-list">
                      {category.topics.map((topic) => (
                        <li key={topic.slug}>
                          <Link to={`/help/${category.slug}/${topic.slug}`}>{topic.title}</Link>
                          <span className="muted"> — {topic.summary}</span>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            </>
          )}
        </article>
      </div>
    </div>
  );
}
