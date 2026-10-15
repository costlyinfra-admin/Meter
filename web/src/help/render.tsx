/**
 * The rendering half of the knowledge base.
 *
 * Inline syntax is deliberately tiny: **bold**, *emphasis*, `code`, and [label](/route).
 * Everything becomes React elements, never innerHTML, so a topic can never
 * inject markup — and an in-app [link](/route) is a real router link rather than
 * a string telling the reader where to click.
 */
import { Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Snippet } from "../components/Snippet";
import { KB_IMAGE_DIR, type Block } from "./blocks";
import { Figure } from "./figures";

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*\s][^*]*\*)/g;

/** Render **bold**, *emphasis*, `code` and [label](/route) into elements. */
export function Inline({ text }: { text: string }): ReactNode {
  return (
    <>
      {text.split(INLINE).map((part, i) => {
        if (!part) return null;
        const key = `${i}-${part.slice(0, 12)}`;
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={key}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("`") && part.endsWith("`")) {
          return <code key={key}>{part.slice(1, -1)}</code>;
        }
        if (part.length > 2 && part.startsWith("*") && part.endsWith("*")) {
          return <em key={key}>{part.slice(1, -1)}</em>;
        }
        const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
        if (link) {
          const [, label, href] = link;
          // In-app destinations route; anything else opens as a normal link.
          return href.startsWith("/") ? (
            <Link key={key} to={href}>
              {label}
            </Link>
          ) : (
            <a key={key} href={href} target="_blank" rel="noreferrer">
              {label}
            </a>
          );
        }
        return <Fragment key={key}>{part}</Fragment>;
      })}
    </>
  );
}

export function Blocks({ blocks }: { blocks: Block[] }) {
  return (
    <>
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "p":
            return (
              <p key={i} className="kb-p">
                <Inline text={block.text} />
              </p>
            );
          case "list":
            return (
              <ul key={i} className="kb-list">
                {block.items.map((item, j) => (
                  <li key={j}>
                    <Inline text={item} />
                  </li>
                ))}
              </ul>
            );
          case "steps":
            return (
              <ol key={i} className="kb-steps">
                {block.items.map((item, j) => (
                  <li key={j}>
                    <span className="kb-step-num" aria-hidden>
                      {j + 1}
                    </span>
                    <span className="kb-step-body">
                      <Inline text={item} />
                    </span>
                  </li>
                ))}
              </ol>
            );
          case "figure":
            return <Figure key={i} name={block.name} caption={block.caption} alt={block.alt} />;
          case "code":
            return <Snippet key={i}>{block.text}</Snippet>;
          case "note":
            return (
              <aside key={i} className="kb-callout">
                <span className="kb-callout-icon" aria-hidden>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="9" />
                    <path d="M12 11v6M12 7.5v.01" strokeLinecap="round" />
                  </svg>
                </span>
                <p>
                  <Inline text={block.text} />
                </p>
              </aside>
            );
          case "image":
            return (
              <figure key={i} className="kb-figure">
                {/* A wide screen in a narrow column is small; a click opens it
                    at full size. */}
                <a href={`${KB_IMAGE_DIR}/${block.file}`} target="_blank" rel="noreferrer">
                  <img src={`${KB_IMAGE_DIR}/${block.file}`} alt={block.alt} loading="lazy" />
                </a>
                {block.caption && (
                  <figcaption>
                    <Inline text={block.caption} />
                  </figcaption>
                )}
              </figure>
            );
          case "table":
            return (
              <div key={i} className="kb-table-wrap">
                <table className="features-table">
                  <thead>
                    <tr>
                      {block.head.map((h) => (
                        <th key={h}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, j) => (
                      <tr key={j}>
                        {row.map((cell, k) => (
                          <td key={k}>
                            <Inline text={cell} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
      })}
    </>
  );
}
