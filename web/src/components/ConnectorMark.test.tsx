import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConnectorMark } from "./ConnectorMark";
import { CONNECTOR_GUIDES } from "../connectorGuides";

const mark = () => document.querySelector(".connector-mark") as HTMLElement;
const logo = () => document.querySelector(".connector-mark img") as HTMLImageElement;

describe("ConnectorMark", () => {
  it("shows the provider's own logo", () => {
    render(<ConnectorMark type="openai" name="OpenAI" />);
    expect(logo()).toBeInTheDocument();
    expect(logo().getAttribute("src")).toMatch(/openai\.svg/);
  });

  it("leaves the logo unnamed, because the name is right beside it", () => {
    render(<ConnectorMark type="anthropic" name="Anthropic" />);
    expect(logo()).toHaveAttribute("alt", "");
  });

  // Connectors we knowingly ship without a mark. An entry here is a debt, not a
  // decision: the row falls back to a monogram until someone drops
  // `src/logos/<type>.svg` in, which needs no code change.
  //
  // We do not draw these ourselves. A hand-approximated logo beside a company's
  // name reads worse than initials, and google.svg is the cautionary case — it
  // is Gemini's sparkle, not Google Cloud's mark, so GCP is on this list rather
  // than wearing the wrong product's logo.
  const NO_LOGO_YET = new Set(["gcp", "digitalocean", "mongodb_atlas", "cloudflare", "snowflake"]);

  it("has a logo for every provider with a setup guide", () => {
    // The list is derived from src/logos, so a missing file is a missing logo
    // and shows up here rather than as one grey tile in a list of two dozen.
    for (const type of Object.keys(CONNECTOR_GUIDES)) {
      if (NO_LOGO_YET.has(type)) continue;
      const { unmount } = render(<ConnectorMark type={type} name={type} />);
      expect(logo(), `no logo file for ${type}`).toBeInTheDocument();
      unmount();
    }
  });

  it("falls back to a readable monogram for a connector still owed its mark", () => {
    for (const type of NO_LOGO_YET) {
      const { unmount } = render(<ConnectorMark type={type} name="Placeholder Name" />);
      expect(logo(), `${type} is on NO_LOGO_YET but has a logo`).toBeNull();
      expect(mark()).toHaveTextContent("PN");
      unmount();
    }
  });

  it("keeps the exception list honest by shrinking it when a mark lands", () => {
    // The assertion above already fails if an entry gains a logo file, which is
    // what makes this list self-cleaning rather than a growing pile of excuses.
    // This one keeps it from covering connectors that no longer exist.
    for (const type of NO_LOGO_YET) {
      expect(Object.keys(CONNECTOR_GUIDES), `${type} is not a connector`).toContain(type);
    }
  });

  it("has a logo for the build-side connectors too", () => {
    for (const type of ["cursor", "okta", "entra"]) {
      const { unmount } = render(<ConnectorMark type={type} name={type} />);
      expect(logo(), `no logo file for ${type}`).toBeInTheDocument();
      unmount();
    }
  });

  it("falls back to initials for a source it has no logo for", () => {
    render(<ConnectorMark type="brand-new" name="Brand New" />);
    expect(logo()).toBeNull();
    expect(mark()).toHaveTextContent("BN");
  });

  it("builds initials from whatever the name gives it", () => {
    const initials = (name: string) => {
      const { unmount } = render(<ConnectorMark type="no-logo" name={name} />);
      const text = mark().textContent;
      unmount();
      return text;
    };
    expect(initials("Together AI")).toBe("TA");
    expect(initials("Modal")).toBe("MO");
    expect(initials("Amazon Bedrock (AWS cost)")).toBe("AB"); // punctuation ignored
    expect(initials("—")).toBe("?"); // nothing to work with
  });
});
