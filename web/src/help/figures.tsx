/**
 * The knowledge base's diagrams.
 *
 * Each is hand-drawn inline SVG rather than an image file, for three reasons:
 * it stays sharp at any size, it follows the light and dark themes (every
 * colour is a class mapped onto the app's tokens in styles.css, never a
 * literal), and its words are real text a screen reader can reach through the
 * figure's description.
 *
 * The colours mean what they mean everywhere else in the product: lavender is
 * build cost and lime is run (inference) cost, as on the Overview's trend;
 * green is measured, amber a ceiling and lavender a tested figure, as on
 * Optimize. A diagram that used them differently would teach the wrong thing.
 *
 * Every figure draws to a 640-unit-wide viewBox. On a phone the figure keeps
 * a readable minimum width and scrolls inside its frame instead of shrinking
 * its labels to nothing.
 */
import { useId, type ReactNode } from "react";
import type { FigureName } from "./blocks";
import "./figures.css";

// ---------------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------------
function useMarker(): [string, ReactNode] {
  // useId's ":r1:" form is awkward inside url(#…); the colons go.
  const id = `kf-arrow-${useId().replace(/:/g, "")}`;
  return [
    `url(#${id})`,
    <defs key="defs">
      <marker
        id={id}
        viewBox="0 0 10 10"
        refX="9"
        refY="5"
        markerWidth="7"
        markerHeight="7"
        orient="auto-start-reverse"
      >
        <path d="M0 0 L10 5 L0 10 z" className="kf-head" />
      </marker>
    </defs>,
  ];
}

type TextProps = {
  x: number;
  y: number;
  c?: string;
  end?: boolean;
  mid?: boolean;
  children: ReactNode;
};

function T({ x, y, c = "kf-t", end, mid, children }: TextProps) {
  return (
    <text x={x} y={y} className={c} textAnchor={end ? "end" : mid ? "middle" : "start"}>
      {children}
    </text>
  );
}

function Svg({ h, label, children }: { h: number; label: string; children: ReactNode }) {
  return (
    <svg viewBox={`0 0 640 ${h}`} className="kf-svg" role="img" aria-label={label}>
      {children}
    </svg>
  );
}

/** A small check mark centred on (x, y). */
function Check({ x, y, c = "kf-stroke-ink" }: { x: number; y: number; c?: string }) {
  return <path d={`M${x - 5} ${y} l3.5 3.5 l6.5 -7`} className={c} />;
}

// ---------------------------------------------------------------------------
// The figures
// ---------------------------------------------------------------------------

/** What Meter does: a blended bill in, cost per feature out. */
function BillSplit({ label }: { label: string }) {
  const [arrow, defs] = useMarker();
  const bills = [
    ["Anthropic", "$18,420", "model calls"],
    ["OpenAI", "$9,310", "model calls"],
    ["Cursor", "$4,200", "coding tools"],
  ];
  const features = [
    ["Threat triage", 118, 196],
    ["Report generator", 70, 92],
    ["SOC copilot", 150, 58],
  ] as const;
  return (
    <Svg h={250} label={label}>
      {defs}
      <T x={16} y={24} c="kf-tl">
        YOUR AI BILLS
      </T>
      {bills.map(([name, amount, what], i) => {
        const y = 38 + i * 64;
        return (
          <g key={name}>
            <rect x={16} y={y} width={164} height={52} rx={10} className="kf-card" />
            <T x={30} y={y + 22} c="kf-t kf-tb">
              {name}
            </T>
            <T x={30} y={y + 39} c="kf-ts">
              {what}
            </T>
            <T x={168} y={y + 31} c="kf-td" end>
              {amount}
            </T>
            <path
              d={`M180 ${y + 26} C 210 ${y + 26}, 206 125, 234 125`}
              className="kf-wire"
              markerEnd={i === 1 ? arrow : undefined}
            />
          </g>
        );
      })}
      <T x={98} y={240} c="kf-ts" mid>
        How much — never what for
      </T>

      <rect x={236} y={95} width={118} height={60} rx={14} className="kf-accent" />
      <T x={295} y={122} c="kf-td kf-on-accent kf-big" mid>
        Meter
      </T>
      <T x={295} y={141} c="kf-ts kf-on-accent" mid>
        disaggregates
      </T>

      <T x={400} y={24} c="kf-tl">
        COST PER FEATURE
      </T>
      <rect x={540} y={14} width={10} height={10} rx={2} className="kf-build" />
      <T x={555} y={23} c="kf-ts">
        Build
      </T>
      <rect x={590} y={14} width={10} height={10} rx={2} className="kf-run" />
      <T x={605} y={23} c="kf-ts">
        Run
      </T>
      {features.map(([name, build, run], i) => {
        const y = 40 + i * 50;
        return (
          <g key={name}>
            <path d={`M354 125 C 378 125, 374 ${y + 22}, 396 ${y + 22}`} className="kf-wire" />
            <T x={400} y={y + 12} c="kf-t kf-tb kf-sm">
              {name}
            </T>
            <rect x={400} y={y + 18} width={build} height={8} rx={3} className="kf-build" />
            <rect x={400} y={y + 29} width={run} height={8} rx={3} className="kf-run" />
          </g>
        );
      })}
      <path d="M354 125 C 378 125, 374 212, 396 212" className="kf-wire kf-dash" />
      <rect x={400} y={200} width={64} height={22} rx={6} className="kf-gap" />
      <T x={474} y={210} c="kf-t kf-tb kf-sm">
        Unattributed
      </T>
      <T x={474} y={224} c="kf-ts">
        its own row, never spread
      </T>
    </Svg>
  );
}

/** Build cost is mostly up front; run cost recurs and grows. */
function BuildVsRun({ label }: { label: string }) {
  const build = [70, 52, 30, 20, 15, 12, 10, 10, 9, 8, 8, 7];
  const run = [8, 14, 20, 28, 35, 43, 51, 59, 66, 74, 82, 90];
  const base = 190;
  return (
    <Svg h={236} label={label}>
      <rect x={30} y={30} width={10} height={10} rx={2} className="kf-build" />
      <T x={46} y={39} c="kf-t kf-tb">
        Build — what it cost to make
      </T>
      <T x={46} y={56} c="kf-ts">
        mostly up front, then a maintenance tail
      </T>
      <rect x={600} y={30} width={10} height={10} rx={2} className="kf-run" />
      <T x={592} y={39} c="kf-t kf-tb" end>
        Run — what it costs to keep
      </T>
      <T x={592} y={56} c="kf-ts" end>
        every month, growing with usage
      </T>
      {build.map((b, i) => {
        const x = 52 + i * 46;
        return (
          <g key={i}>
            <rect x={x} y={base - b} width={17} height={b} rx={3} className="kf-build" />
            <rect
              x={x + 19}
              y={base - run[i]}
              width={17}
              height={run[i]}
              rx={3}
              className="kf-run"
            />
            <T x={x + 18} y={base + 18} c="kf-ts" mid>
              {`M${i + 1}`}
            </T>
          </g>
        );
      })}
      <line x1={44} x2={604} y1={base} y2={base} className="kf-axis" />
      <rect x={226} y={78} width={188} height={26} rx={13} className="kf-soft" />
      <T x={320} y={95} c="kf-t kf-tb kf-sm" mid>
        Two numbers. Never added together.
      </T>
      <T x={320} y={228} c="kf-ts" mid>
        months since the feature shipped
      </T>
    </Svg>
  );
}

/** A feature is what every cost and every call hangs off. */
function FeatureSpine({ label }: { label: string }) {
  const spokes = [
    { x: 16, y: 22, title: "Build cost", sub: "AI coding tools, by PR author", swatch: "kf-build" },
    { x: 444, y: 22, title: "Inference cost", sub: "model calls in production", swatch: "kf-run" },
    { x: 16, y: 150, title: "Usage", sub: "calls, tokens, customers", swatch: "kf-teal" },
  ];
  return (
    <Svg h={222} label={label}>
      {spokes.map((s) => (
        <g key={s.title}>
          <line
            x1={s.x < 300 ? s.x + 180 : s.x}
            y1={s.y + 30}
            x2={320}
            y2={111}
            className="kf-wire"
          />
          <rect x={s.x} y={s.y} width={180} height={60} rx={12} className="kf-card" />
          <rect x={s.x + 14} y={s.y + 16} width={10} height={10} rx={2} className={s.swatch} />
          <T x={s.x + 32} y={s.y + 26} c="kf-t kf-tb">
            {s.title}
          </T>
          <T x={s.x + 14} y={s.y + 45} c="kf-ts">
            {s.sub}
          </T>
        </g>
      ))}
      <line x1={444} y1={180} x2={320} y2={111} className="kf-wire kf-dash" />
      <rect x={444} y={150} width={180} height={60} rx={12} className="kf-gap" />
      <T x={458} y={176} c="kf-t kf-tb">
        Unattributed
      </T>
      <T x={458} y={195} c="kf-ts">
        anything that fits no feature
      </T>
      <rect x={250} y={81} width={140} height={60} rx={16} className="kf-accent" />
      <T x={320} y={108} c="kf-td kf-on-accent kf-big" mid>
        Feature
      </T>
      <T x={320} y={127} c="kf-ts kf-on-accent" mid>
        the spine
      </T>
    </Svg>
  );
}

/** The bill, split by feature, with the honest remainder kept visible. */
function Unattributed({ label }: { label: string }) {
  const parts = [
    { name: "Threat triage", amount: "$380", w: 231, c: "kf-teal" },
    { name: "Reports", amount: "$240", w: 146, c: "kf-gold" },
    { name: "SOC copilot", amount: "$220", w: 134, c: "kf-coral" },
  ];
  let x = 16;
  return (
    <Svg h={206} label={label}>
      <T x={16} y={24} c="kf-tl">
        THIS MONTH&rsquo;S PROVIDER BILL — $1,000
      </T>
      {parts.map((p) => {
        const at = x;
        x += p.w;
        return (
          <g key={p.name}>
            <rect x={at} y={36} width={p.w - 3} height={46} rx={8} className={p.c} />
            <T x={at + 12} y={56} c="kf-t kf-tb kf-sm kf-on-fill">
              {p.name}
            </T>
            <T x={at + 12} y={72} c="kf-ts kf-on-fill">
              {p.amount}
            </T>
          </g>
        );
      })}
      <rect x={x} y={36} width={624 - x} height={46} rx={8} className="kf-gap" />
      <T x={x + 10} y={56} c="kf-t kf-tb kf-sm">
        Unattributed
      </T>
      <T x={x + 10} y={72} c="kf-ts">
        $160
      </T>
      <path d={`M${x + 48} 84 V 118`} className="kf-wire kf-dash" />
      <rect x={352} y={120} width={272} height={74} rx={12} className="kf-card" />
      <T x={366} y={142} c="kf-t kf-tb kf-sm">
        Why spend lands here
      </T>
      <T x={366} y={161} c="kf-ts">
        calls with no feature tag · a reconciliation gap
      </T>
      <T x={366} y={178} c="kf-ts">
        an unmatched PR author · a deleted feature
      </T>
      <T x={16} y={140} c="kf-t kf-tb">
        Every dollar lands somewhere.
      </T>
      <T x={16} y={160} c="kf-ts">
        On a feature, or visibly on Unattributed —
      </T>
      <T x={16} y={176} c="kf-ts">
        never spread across features to tidy the totals.
      </T>
    </Svg>
  );
}

/** Dollars from the provider, resolution from metering, the gap to Unattributed. */
function Reconciliation({ label }: { label: string }) {
  const [arrow, defs] = useMarker();
  return (
    <Svg h={236} label={label}>
      {defs}
      <rect x={16} y={20} width={214} height={80} rx={12} className="kf-card" />
      <rect x={16} y={20} width={6} height={80} rx={3} className="kf-teal" />
      <T x={34} y={42} c="kf-tl">
        PROVIDER COST API
      </T>
      <T x={34} y={62} c="kf-t kf-tb kf-sm">
        Authoritative on dollars
      </T>
      <T x={34} y={86} c="kf-td">
        $1,000 billed
      </T>

      <rect x={16} y={134} width={214} height={80} rx={12} className="kf-card" />
      <rect x={16} y={134} width={6} height={80} rx={3} className="kf-run" />
      <T x={34} y={156} c="kf-tl">
        METERING SDK
      </T>
      <T x={34} y={176} c="kf-t kf-tb kf-sm">
        Authoritative on resolution
      </T>
      <T x={34} y={200} c="kf-td">
        $940 metered, by feature
      </T>

      <path d="M230 60 C 256 60, 254 117, 276 117" className="kf-wire" markerEnd={arrow} />
      <path d="M230 174 C 256 174, 254 117, 276 117" className="kf-wire" markerEnd={arrow} />
      <rect x={280} y={88} width={112} height={58} rx={14} className="kf-accent" />
      <T x={336} y={114} c="kf-td kf-on-accent" mid>
        Reconcile
      </T>
      <T x={336} y={132} c="kf-ts kf-on-accent" mid>
        every period
      </T>

      <path d="M392 117 C 414 117, 412 60, 432 60" className="kf-wire" markerEnd={arrow} />
      <path d="M392 117 C 414 117, 412 174, 432 174" className="kf-wire" markerEnd={arrow} />
      <rect x={436} y={20} width={188} height={80} rx={12} className="kf-card" />
      <T x={450} y={42} c="kf-t kf-tb kf-sm">
        Per-feature split
      </T>
      <T x={450} y={59} c="kf-ts">
        sized to the bill
      </T>
      <rect x={450} y={70} width={70} height={14} rx={4} className="kf-teal" />
      <rect x={522} y={70} width={44} height={14} rx={4} className="kf-gold" />
      <rect x={568} y={70} width={42} height={14} rx={4} className="kf-coral" />

      <rect x={436} y={134} width={188} height={80} rx={12} className="kf-gap" />
      <T x={450} y={158} c="kf-t kf-tb kf-sm">
        Unattributed: $60
      </T>
      <T x={450} y={176} c="kf-ts">
        the bill minus what was
      </T>
      <T x={450} y={192} c="kf-ts">
        metered: a lost event lowers
      </T>
      <T x={450} y={207} c="kf-ts">
        attribution, never the bill
      </T>
    </Svg>
  );
}

/** Three levels, and a feature is only as sure as its weakest part. */
function Confidence({ label }: { label: string }) {
  const rows = [
    ["HIGH", 3, "kf-good", "Metered per call, or read straight from a provider cost API"],
    ["MED", 2, "kf-warn", "A strong but indirect signal, such as who authored the PR"],
    ["LOW", 1, "kf-coral", "Inferred from a weak signal — treat it as directional"],
  ] as const;
  return (
    <Svg h={250} label={label}>
      {rows.map(([level, dots, tone, text], i) => {
        const y = 16 + i * 54;
        return (
          <g key={level}>
            <rect x={16} y={y} width={608} height={44} rx={10} className="kf-card" />
            {[0, 1, 2].map((d) => (
              <circle
                key={d}
                cx={38 + d * 16}
                cy={y + 22}
                r={5.5}
                className={d < dots ? tone : "kf-empty"}
              />
            ))}
            <T x={92} y={y + 27} c="kf-t kf-tb">
              {level}
            </T>
            <T x={144} y={y + 27} c="kf-t kf-sm">
              {text}
            </T>
          </g>
        );
      })}
      <T x={16} y={198} c="kf-tl">
        A FEATURE TAKES ITS LOWEST PART
      </T>
      {[
        ["Inference", "HIGH", 16],
        ["Build", "MED", 176],
      ].map(([what, level, x]) => (
        <g key={what as string}>
          <rect x={x as number} y={210} width={140} height={30} rx={15} className="kf-soft" />
          <T x={(x as number) + 70} y={230} c="kf-t kf-sm" mid>
            {`${what} · ${level}`}
          </T>
        </g>
      ))}
      <T x={164} y={230} c="kf-t kf-tb" mid>
        +
      </T>
      <T x={334} y={230} c="kf-t kf-tb" mid>
        =
      </T>
      <rect x={352} y={210} width={160} height={30} rx={15} className="kf-accent" />
      <T x={432} y={230} c="kf-t kf-tb kf-sm kf-on-accent" mid>
        Feature · MED
      </T>
      <T x={526} y={230} c="kf-ts">
        not an average
      </T>
    </Svg>
  );
}

/** What leaves your servers, and what never does. */
function SdkData({ label }: { label: string }) {
  const [arrow, defs] = useMarker();
  return (
    <Svg h={250} label={label}>
      {defs}
      <rect x={16} y={14} width={340} height={222} rx={18} className="kf-zone" />
      <T x={34} y={38} c="kf-tl">
        YOUR SERVERS
      </T>
      <rect x={34} y={58} width={128} height={50} rx={10} className="kf-card" />
      <T x={98} y={88} c="kf-t kf-tb" mid>
        Your app
      </T>
      <path d="M162 83 H 192" className="kf-wire" markerEnd={arrow} />
      <rect x={196} y={58} width={140} height={50} rx={10} className="kf-run" />
      <T x={266} y={81} c="kf-t kf-tb kf-on-lime" mid>
        Meter SDK
      </T>
      <T x={266} y={97} c="kf-ts kf-on-lime" mid>
        wraps your client
      </T>

      <rect x={34} y={136} width={302} height={82} rx={12} className="kf-card" />
      <rect x={52} y={162} width={20} height={16} rx={3} className="kf-ink" />
      <path d="M56 162 v-5 a6 6 0 0 1 12 0 v5" className="kf-stroke-ink" />
      <T x={86} y={166} c="kf-t kf-tb kf-sm">
        Prompts, responses, source code
      </T>
      <T x={86} y={184} c="kf-ts">
        stay here. Prices too: Meter computes
      </T>
      <T x={86} y={199} c="kf-ts">
        cost on its side from the token counts.
      </T>

      <path d="M336 72 C 380 72, 380 54, 418 54" className="kf-wire" markerEnd={arrow} />
      <T x={377} y={38} c="kf-ts" mid>
        the call itself
      </T>
      <rect x={422} y={30} width={202} height={50} rx={10} className="kf-card" />
      <T x={523} y={53} c="kf-t kf-tb" mid>
        Model provider
      </T>
      <T x={523} y={69} c="kf-ts" mid>
        direct — the SDK is not a proxy
      </T>

      <path d="M336 96 C 384 96, 380 168, 418 168" className="kf-wire kf-dash" markerEnd={arrow} />
      <rect x={350} y={118} width={92} height={22} rx={11} className="kf-soft" />
      <T x={396} y={133} c="kf-ts kf-tb" mid>
        numbers only
      </T>
      <rect x={422} y={136} width={202} height={78} rx={12} className="kf-accent" />
      <T x={438} y={160} c="kf-td kf-on-accent">
        Meter
      </T>
      <T x={438} y={180} c="kf-ts kf-on-accent">
        model · tokens in and out
      </T>
      <T x={438} y={196} c="kf-ts kf-on-accent">
        latency · feature id
      </T>
    </Svg>
  );
}

/** The four kinds of saving, from certain to merely worth a look. */
function SavingsKinds({ label }: { label: string }) {
  const kinds = [
    {
      title: "Measured",
      tone: "kf-good",
      lines: ["Seen in your traffic,", "priced at list rate"],
    },
    {
      title: "Tested",
      tone: "kf-tested",
      lines: ["Held up on your test", "cases; its own figure"],
    },
    {
      title: "Modeled ceiling",
      tone: "kf-warn",
      lines: ["An upper bound: the", "count is real, the", "saving is not"],
    },
    {
      title: "Not quantified",
      tone: "kf-muted",
      lines: ["Spend worth a review —", "no claim of a saving"],
    },
  ];
  return (
    <Svg h={212} label={label}>
      {kinds.map((k, i) => {
        const x = 16 + i * 156;
        return (
          <g key={k.title}>
            <rect x={x} y={14} width={144} height={176} rx={14} className="kf-card" />
            <circle cx={x + 26} cy={42} r={12} className={k.tone} />
            {i === 0 && <Check x={x + 26} y={42} c="kf-stroke-onfill" />}
            {i === 1 && (
              <path
                d={`M${x + 22} ${35} h8 M${x + 24} ${35} v6 l-5 8 h14 l-5 -8 v-6`}
                className="kf-stroke-onfill"
              />
            )}
            {i === 2 && <path d={`M${x + 20} ${42} h12`} className="kf-stroke-onfill" />}
            {i === 3 && (
              <T x={x + 26} y={47} c="kf-t kf-tb kf-on-fill" mid>
                ?
              </T>
            )}
            <T x={x + 14} y={82} c="kf-td">
              {k.title}
            </T>
            {k.lines.map((line, j) => (
              <T key={line} x={x + 14} y={104 + j * 16} c="kf-ts">
                {line}
              </T>
            ))}
            {i === 0 && (
              <rect x={x + 14} y={152} width={116} height={14} rx={4} className="kf-good" />
            )}
            {i === 1 && (
              <rect x={x + 14} y={152} width={92} height={14} rx={4} className="kf-tested" />
            )}
            {i === 2 && (
              <>
                <rect x={x + 14} y={152} width={116} height={14} rx={4} className="kf-outline" />
                <rect x={x + 14} y={152} width={52} height={14} rx={4} className="kf-warn" />
                <T x={x + 126} y={180} c="kf-ts" end>
                  up to
                </T>
              </>
            )}
            {i === 3 && (
              <rect x={x + 14} y={152} width={116} height={14} rx={4} className="kf-outline" />
            )}
          </g>
        );
      })}
      <path d="M16 202 H 624" className="kf-axis" />
      <T x={16} y={210} c="kf-ts">
        more certain
      </T>
      <T x={624} y={210} c="kf-ts" end>
        less certain
      </T>
    </Svg>
  );
}

/** Four ways to test a recommendation, one rule to decide it. */
function TestWays({ label }: { label: string }) {
  const [arrow, defs] = useMarker();
  const ways = [
    {
      title: "Simulation",
      lines: ["Caching and repeats", "Counts the SDK sends", "Costs nothing"],
    },
    {
      title: "Your own cases",
      lines: ["Runs on your machine", "Your keys, your judge", "Numbers come back"],
    },
    {
      title: "Run by Meter",
      lines: ["On calls it captured", "with your consent", "Cost shown first"],
    },
    {
      title: "Live traffic",
      lines: ["Your feature flag", "splits real requests", "Guardrails stop it"],
    },
  ];
  return (
    <Svg h={300} label={label}>
      {defs}
      {ways.map((w, i) => {
        const x = 16 + i * 156;
        return (
          <g key={w.title}>
            <rect x={x} y={14} width={144} height={128} rx={14} className="kf-card" />
            <T x={x + 14} y={36} c="kf-tl">
              {`WAY ${i + 1}`}
            </T>
            <T x={x + 14} y={58} c="kf-td">
              {w.title}
            </T>
            {w.lines.map((line, j) => (
              <T key={line} x={x + 14} y={82 + j * 17} c="kf-ts">
                {line}
              </T>
            ))}
            <path d={`M${x + 72} 142 C ${x + 72} 162, 320 158, 320 174`} className="kf-wire" />
          </g>
        );
      })}
      <path d="M320 170 V 176" className="kf-wire" markerEnd={arrow} />
      <rect x={16} y={180} width={608} height={44} rx={12} className="kf-accent" />
      <T x={320} y={200} c="kf-t kf-tb kf-on-accent" mid>
        One rule decides
      </T>
      <T x={320} y={216} c="kf-ts kf-on-accent" mid>
        nothing broken · not worse beyond the allowance · enough cases · actually cheaper
      </T>
      {[
        ["Passed", "becomes a tested figure", "kf-good", 16],
        ["Failed", "card kept, out of the totals", "kf-warn", 224],
        ["Inconclusive", "nothing changes", "kf-muted", 432],
      ].map(([title, sub, tone, x]) => (
        <g key={title as string}>
          <rect x={x as number} y={240} width={192} height={46} rx={12} className="kf-card" />
          <circle cx={(x as number) + 20} cy={263} r={6} className={tone as string} />
          <T x={(x as number) + 34} y={259} c="kf-t kf-tb kf-sm">
            {title}
          </T>
          <T x={(x as number) + 34} y={276} c="kf-ts">
            {sub}
          </T>
        </g>
      ))}
    </Svg>
  );
}

/** Projected, realized, verified — and the prediction held against the bill. */
function ProveLoop({ label }: { label: string }) {
  const stages = [
    {
      x: 110,
      title: "Projected",
      lines: ["frozen when you mark", "the change applied"],
      tone: "kf-warn",
    },
    {
      x: 320,
      title: "Realized",
      lines: ["the drop your telemetry", "measures since"],
      tone: "kf-teal",
    },
    {
      x: 530,
      title: "Verified",
      lines: ["held two months, and", "the bill agrees"],
      tone: "kf-good",
    },
  ];
  return (
    <Svg h={238} label={label}>
      <line x1={110} x2={530} y1={58} y2={58} className="kf-track" />
      {stages.map((s, i) => (
        <g key={s.title}>
          <circle cx={s.x} cy={58} r={22} className={s.tone} />
          {i === 2 ? (
            <Check x={s.x} y={58} c="kf-stroke-onfill" />
          ) : (
            <T x={s.x} y={64} c="kf-td kf-on-fill" mid>
              {String(i + 1)}
            </T>
          )}
          <T x={s.x} y={102} c="kf-td" mid>
            {s.title}
          </T>
          {s.lines.map((line, j) => (
            <T key={line} x={s.x} y={122 + j * 16} c="kf-ts" mid>
              {line}
            </T>
          ))}
        </g>
      ))}
      <rect x={120} y={170} width={400} height={54} rx={14} className="kf-card" />
      <rect x={120} y={170} width={6} height={54} rx={3} className="kf-run" />
      <T x={140} y={192} c="kf-tl">
        PREDICTED VS BILL
      </T>
      <T x={140} y={212} c="kf-t kf-sm">
        −18% predicted per call · −15% on the bill
      </T>
      <rect x={410} y={186} width={98} height={24} rx={12} className="kf-run" />
      <T x={459} y={202} c="kf-ts kf-tb kf-on-lime" mid>
        85% arrived
      </T>
    </Svg>
  );
}

/** Actual spend, then a projection whose range widens, against the budget. */
function ForecastCone({ label }: { label: string }) {
  const actual = [150, 142, 136, 125, 118, 108, 100];
  const step = 62;
  const x0 = 40;
  const pts = actual.map((y, i) => `${x0 + i * step},${y}`).join(" ");
  const last = { x: x0 + 6 * step, y: 100 };
  const proj = [
    [last.x + step, 90, 10],
    [last.x + step * 2, 80, 20],
    [last.x + step * 3, 70, 32],
  ];
  const upper = [`${last.x},${last.y}`, ...proj.map(([x, y, r]) => `${x},${y - r}`)];
  const lower = [...proj.map(([x, y, r]) => `${x},${y + r}`).reverse(), `${last.x},${last.y}`];
  return (
    <Svg h={220} label={label}>
      <line x1={30} x2={620} y1={60} y2={60} className="kf-budget" />
      <T x={36} y={52} c="kf-ts kf-tb">
        Monthly budget
      </T>
      <polygon points={[...upper, ...lower].join(" ")} className="kf-band" />
      <polyline points={pts} className="kf-line" />
      <polyline
        points={[`${last.x},${last.y}`, ...proj.map(([x, y]) => `${x},${y}`)].join(" ")}
        className="kf-line kf-dash"
      />
      {actual.map((y, i) => (
        <circle key={i} cx={x0 + i * step} cy={y} r={3.5} className="kf-dot" />
      ))}
      <line x1={last.x} x2={last.x} y1={30} y2={182} className="kf-axis kf-dash" />
      <T x={last.x - 8} y={40} c="kf-ts" end>
        actual
      </T>
      <T x={last.x + 8} y={40} c="kf-ts">
        projected
      </T>
      <T x={proj[2][0] - 4} y={proj[2][1] + proj[2][2] + 18} c="kf-ts" end>
        80% range — widens with distance
      </T>
      <line x1={30} x2={620} y1={182} y2={182} className="kf-axis" />
      {["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan"].map((m, i) => (
        <T key={m} x={x0 + i * step} y={200} c="kf-ts" mid>
          {m}
        </T>
      ))}
      <T x={x0} y={140} c="kf-ts">
        spend
      </T>
    </Svg>
  );
}

/** The invoice you were sent, lined up against what Meter tracked. */
function InvoiceCheck({ label }: { label: string }) {
  const [arrow, defs] = useMarker();
  const outcomes = [
    ["Matched", "kf-good"],
    ["Within tolerance", "kf-teal"],
    ["Discrepancy", "kf-warn"],
    ["Incomplete data", "kf-muted"],
  ];
  return (
    <Svg h={206} label={label}>
      {defs}
      <rect x={16} y={18} width={186} height={96} rx={12} className="kf-card" />
      {[0, 1, 2, 3].map((r) => (
        <rect
          key={r}
          x={32}
          y={50 + r * 14}
          width={r === 3 ? 90 : 150}
          height={6}
          rx={3}
          className="kf-empty"
        />
      ))}
      <T x={32} y={40} c="kf-t kf-tb kf-sm">
        Your provider&rsquo;s invoice
      </T>
      <rect x={438} y={18} width={186} height={96} rx={12} className="kf-card" />
      <T x={454} y={40} c="kf-t kf-tb kf-sm">
        What Meter tracked
      </T>
      <rect x={454} y={54} width={60} height={12} rx={3} className="kf-teal" />
      <rect x={516} y={54} width={40} height={12} rx={3} className="kf-gold" />
      <rect x={558} y={54} width={36} height={12} rx={3} className="kf-coral" />
      <T x={454} y={92} c="kf-ts">
        same period, same provider
      </T>
      <path d="M202 66 H 262" className="kf-wire" markerEnd={arrow} />
      <path d="M438 66 H 378" className="kf-wire" markerEnd={arrow} />
      <rect x={266} y={36} width={108} height={60} rx={14} className="kf-accent" />
      <T x={320} y={62} c="kf-td kf-on-accent" mid>
        Line up
      </T>
      <T x={320} y={80} c="kf-ts kf-on-accent" mid>
        explain each gap
      </T>
      <path d="M320 96 V 128" className="kf-wire" markerEnd={arrow} />
      {outcomes.map(([name, tone], i) => {
        const x = 16 + i * 154;
        return (
          <g key={name}>
            <rect x={x} y={136} width={146} height={40} rx={20} className="kf-card" />
            <circle cx={x + 20} cy={156} r={6} className={tone} />
            <T x={x + 34} y={161} c="kf-t kf-sm">
              {name}
            </T>
          </g>
        );
      })}
      <T x={320} y={198} c="kf-ts" mid>
        Tolerances: $1.00 or 0.5% by default — and stored with every run
      </T>
    </Svg>
  );
}

/** A rule, checked on a schedule, raises an incident, delivered where you want it. */
function AlertFlow({ label }: { label: string }) {
  const [arrow, defs] = useMarker();
  const steps = [
    ["Rule", "metric · scope ·", "condition · window"],
    ["Checked", "on a schedule,", "against its window"],
    ["Incident", "a cooldown, then", "a recovery notice"],
  ];
  const channels = ["In-app", "Email", "Slack", "Webhook"];
  return (
    <Svg h={196} label={label}>
      {defs}
      {steps.map(([title, a, b], i) => {
        const x = 16 + i * 152;
        return (
          <g key={title}>
            <rect
              x={x}
              y={58}
              width={128}
              height={74}
              rx={14}
              className={i === 2 ? "kf-accent" : "kf-card"}
            />
            <T x={x + 14} y={84} c={i === 2 ? "kf-td kf-on-accent" : "kf-td"}>
              {title}
            </T>
            <T x={x + 14} y={104} c={i === 2 ? "kf-ts kf-on-accent" : "kf-ts"}>
              {a}
            </T>
            <T x={x + 14} y={119} c={i === 2 ? "kf-ts kf-on-accent" : "kf-ts"}>
              {b}
            </T>
            {i < 2 && (
              <path d={`M${x + 128} 95 H ${x + 148}`} className="kf-wire" markerEnd={arrow} />
            )}
          </g>
        );
      })}
      {channels.map((c, i) => {
        const y = 14 + i * 44;
        return (
          <g key={c}>
            <path
              d={`M424 95 C 456 95, 452 ${y + 18}, 484 ${y + 18}`}
              className="kf-wire"
              markerEnd={arrow}
            />
            <rect x={488} y={y} width={136} height={36} rx={18} className="kf-card" />
            <circle cx={508} cy={y + 18} r={5} className="kf-run" />
            <T x={522} y={y + 23} c="kf-t kf-sm">
              {c}
            </T>
          </g>
        );
      })}
    </Svg>
  );
}

const FIGURES: Record<FigureName, (props: { label: string }) => JSX.Element> = {
  "bill-split": BillSplit,
  "build-vs-run": BuildVsRun,
  "feature-spine": FeatureSpine,
  unattributed: Unattributed,
  reconciliation: Reconciliation,
  confidence: Confidence,
  "sdk-data": SdkData,
  "savings-kinds": SavingsKinds,
  "test-ways": TestWays,
  "prove-loop": ProveLoop,
  "forecast-cone": ForecastCone,
  "invoice-check": InvoiceCheck,
  "alert-flow": AlertFlow,
};

/** A diagram with its caption. `alt` is what a screen reader hears instead. */
export function Figure({ name, caption, alt }: { name: FigureName; caption: string; alt: string }) {
  const Art = FIGURES[name];
  return (
    <figure className="kb-diagram">
      <div className="kb-diagram-art">
        <Art label={alt} />
      </div>
      <figcaption>{caption}</figcaption>
    </figure>
  );
}
