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

  // Connectors we knowingly ship without a mark, and why. An entry here is a
  // debt, not a decision: it costs the row its logo, so it should be short.
  // "gcp": Google Cloud's own mark is not in the repo, and google.svg is
  // Gemini's sparkle — the wrong product's logo beside "Google Cloud Platform"
  // is worse than the monogram, so GCP shows "GC" until the real mark lands.
  const NO_LOGO_YET = new Set(["gcp"]);

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
      const { unmount } = render(<ConnectorMark type={type} name="Google Cloud Platform" />);
      expect(logo()).toBeNull();
      expect(mark()).toHaveTextContent("GC");
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
