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

  // Connectors we knowingly ship without a mark. Empty, and the assertions below
  // are what keep it that way: an entry that gains a logo file fails, so the
  // list shrinks itself rather than becoming a pile of excuses.
  //
  // We still do not draw these ourselves. A hand-approximated logo beside a
  // company's name reads worse than initials — google.svg is the cautionary
  // case, being Gemini's sparkle rather than Google Cloud's mark, which is why
  // GCP has its own file now instead of borrowing that one.
  const NO_LOGO_YET = new Set<string>([]);

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

  it("keeps the exception list honest: an entry that gains a mark must be removed", () => {
    for (const type of NO_LOGO_YET) {
      const { unmount } = render(<ConnectorMark type={type} name="Placeholder Name" />);
      expect(logo(), `${type} is on NO_LOGO_YET but now has a logo — drop it`).toBeNull();
      expect(mark()).toHaveTextContent("PN");
      unmount();
      // And the list must not outlive the connector it excuses.
      expect(Object.keys(CONNECTOR_GUIDES), `${type} is not a connector`).toContain(type);
    }
  });

  it("has a mark for every infrastructure provider", () => {
    // The tab lists eleven sources side by side; one grey monogram among ten
    // logos reads as a mistake rather than as a provider without artwork.
    for (const type of [
      "aws",
      "azure_cloud",
      "gcp",
      "digitalocean",
      "mongodb_atlas",
      "cloudflare",
      "snowflake",
      "vercel_cloud",
      "redis_cloud",
      "supabase",
      "neon",
    ]) {
      const { unmount } = render(<ConnectorMark type={type} name={type} />);
      expect(logo(), `no logo file for ${type}`).toBeInTheDocument();
      unmount();
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
