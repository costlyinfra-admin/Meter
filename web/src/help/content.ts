/**
 * The knowledge base, as data.
 *
 * Organised like a book: categories in reading order, each with topics. Every
 * statement here describes what Meter actually does today — if a behaviour
 * changes, the topic that documents it changes with it. Where a topic makes a
 * claim about a number, it says where the number comes from, because that is the
 * product's whole premise.
 */
import { code, list, note, p, steps, table, type Block } from "./blocks";

export interface Topic {
  slug: string;
  title: string;
  summary: string;
  blocks: Block[];
}

export interface Category {
  slug: string;
  title: string;
  blurb: string;
  topics: Topic[];
}

export const CATEGORIES: Category[] = [
  {
    slug: "getting-started",
    title: "Getting started",
    blurb: "What Meter is for, and how to get your first real numbers.",
    topics: [
      {
        slug: "what-meter-does",
        title: "What Meter does",
        summary: "Turns one blended AI bill into per-feature cost to build and cost to run.",
        blocks: [
          p(
            "Your AI spend arrives as a handful of invoices: some model providers, some coding tools. It tells you **how much**, never **what for**. Meter disaggregates that bill into the thing you can actually make decisions about — cost **per feature**.",
          ),
          p("Every feature gets two numbers, and they are never added together:"),
          list(
            "**Build cost** — what it cost to *make*: the AI coding tools your developers used, attributed by who authored the pull requests behind the feature.",
            "**Inference cost** — what it costs to *run*: the model calls the feature makes in production.",
          ),
          p(
            "They stay separate because they answer different questions. Build cost is largely one-off and tells you what a feature cost to ship. Inference cost recurs every month and tells you what it costs to keep. A single blended number would hide both.",
          ),
          note(
            "Meter never invents a number. Provider bills are authoritative on dollars; anything Meter cannot attribute is shown in an **Unattributed** bucket rather than quietly spread across your features.",
          ),
        ],
      },
      {
        slug: "setup",
        title: "Setting up",
        summary: "The minimum to get real numbers: GitHub plus one AI provider.",
        blocks: [
          p(
            "You need two connections to see something meaningful. Everything else is optional and can be added later.",
          ),
          steps(
            "**Connect GitHub** on [Features](/features). Meter reads your merged pull requests to work out what features exist. Read-only, and a token is optional for public organisations.",
            "**Discover features.** Pick the repositories to analyse and run discovery. You get a list of proposed features, each with the pull requests behind it as evidence. Rename, split, merge or delete them until the list looks like your product.",
            "**Connect a provider** on [Cost sources](/cost-sources) — Anthropic, OpenAI, or whichever you use. Meter reads its cost API. Read-only, using your own admin credentials, stored encrypted.",
            "**Sync.** The first sync backfills twelve months so you have history immediately, not in a year.",
          ),
          p(
            "That is enough for the [Overview](/) to show per-feature build and inference cost. Two optional additions sharpen it: importing a coding-tool spend CSV gives you real build cost, and installing the metering SDK gives per-call precision on inference.",
          ),
          note(
            "No connection is required to look around — the demo account has a fully populated tenant if you just want to see the shape of it.",
          ),
        ],
      },
      {
        slug: "first-dashboard",
        title: "Reading your first dashboard",
        summary: "What the Overview is telling you, in the order it tells you.",
        blocks: [
          p("The [Overview](/) is arranged as an argument, top to bottom."),
          list(
            "**The summary strip** — your most expensive feature, the biggest optimization lever, the highest cost per user, and how much spend is still unattributed.",
            "**Key insights** — plain-language observations generated from your own numbers: an unusually expensive day, a spending pace, a concentration, spend on non-production keys. Each names the figure behind it.",
            "**The totals** — build cost, inference cost and token volume for the selected period, each with its change against the previous one.",
            "**The breakdown tabs** — the same period sliced By Feature, By Provider, By Developer and By Customer.",
          ),
          p(
            "The period selector at the top of the tabs drives everything below it. **This month** means the actual calendar month to date, not a rolling window.",
          ),
        ],
      },
    ],
  },

  {
    slug: "concepts",
    title: "Core concepts",
    blurb: "The five ideas the rest of the product is built on.",
    topics: [
      {
        slug: "build-vs-inference",
        title: "Build cost vs inference cost",
        summary: "Two different questions, never blended into one number.",
        blocks: [
          p(
            "**Build cost** is what your team spent on AI tooling — Claude Code, Cursor, Copilot, fine-tuning runs — attributed to the features those developers were working on. It is mostly one-off, though features keep accruing it as they are maintained.",
          ),
          p(
            "**Inference cost** is what your features spend calling models in production. It recurs every month and scales with usage.",
          ),
          p(
            "Meter stores and displays them separately everywhere, including in the charts. A feature that cost $4,000 to build and $50/month to run is a very different proposition from one that cost $50 to build and $4,000/month to run, and one blended number cannot tell you which you have.",
          ),
        ],
      },
      {
        slug: "features-are-the-spine",
        title: "Features are the spine",
        summary: "Everything attributes to a feature, or to Unattributed. Nothing is dropped.",
        blocks: [
          p(
            "A feature is the unit everything hangs off. Build cost attributes to one, inference cost attributes to one, usage attributes to one.",
          ),
          p(
            "Features come from your merged pull requests. Meter clusters them into proposed features, and you confirm, rename, split or merge until the list matches how you actually think about your product. See [How discovery works](/help/features/discovery).",
          ),
          note(
            "Anything that cannot be attributed to a feature goes to **Unattributed** — visibly, as its own row. It is never spread across features to make the totals look tidy.",
          ),
        ],
      },
      {
        slug: "unattributed",
        title: "The Unattributed bucket",
        summary: "Where honest gaps go, and what each kind of gap means.",
        blocks: [
          p(
            "Spend lands in Unattributed for a few distinct reasons, and they mean different things:",
          ),
          table(
            ["Cause", "What it means", "What to do"],
            [
              [
                "Model calls with no feature tag",
                "Real spend the SDK could not attribute, or a provider key not mapped to a feature",
                "Install or extend the [metering SDK](/install-sdk)",
              ],
              [
                "Reconciliation gap",
                "Your provider bill is higher than the sum of metered calls — untagged traffic or a model priced differently",
                "Usually expected; investigate if it grows",
              ],
              [
                "Build spend with no matching PR author",
                "A developer in your tooling CSV whose GitHub handle Meter could not match",
                "Check the handle in the CSV",
              ],
              [
                "Features deleted after attribution",
                "Cost was attached to a feature that no longer exists",
                "Re-run discovery; attribution is recalculated",
              ],
            ],
          ),
          p(
            "A small Unattributed figure is normal and healthy. A large or growing one means your attribution is thinning out, which is why it appears in the summary strip rather than being buried.",
          ),
        ],
      },
      {
        slug: "reconciliation",
        title: "Reconciliation: why the bill is always right",
        summary: "Provider cost APIs are authoritative; metering adds resolution, never dollars.",
        blocks: [
          p("Meter has two sources of truth about inference, and they do different jobs:"),
          list(
            "**The provider's cost API** is authoritative on **dollars**. It is what you will actually be invoiced.",
            "**Metered calls from the SDK** are authoritative on **resolution** — which feature, which customer, which model made each call.",
          ),
          p(
            "Every period, the metered total is compared against the provider total. If they tie out, the per-feature split is trustworthy. If the bill is higher, the difference goes to Unattributed. The provider API keeps metering honest; metering gives the provider API detail.",
          ),
          note(
            "This is why losing a few metered events never corrupts your bill — it only reduces how much of the bill is attributed. Under-counting is caught by design.",
          ),
          p(
            "This reconciliation is internal and automatic. Checking Meter against the invoice your provider actually sent is a separate, opt-in feature — see [Invoice reconciliation](/help/reconciliation/what-it-is).",
          ),
        ],
      },
      {
        slug: "confidence",
        title: "Confidence",
        summary: "Every cost row carries how much to trust it, and why.",
        blocks: [
          p("Each cost row carries a confidence badge, driven by how the number was derived."),
          table(
            ["Confidence", "Typically means"],
            [
              [
                "**High**",
                "Metered per call by the SDK, or read directly from a provider cost API",
              ],
              ["**Med**", "Attributed by a strong but indirect signal, such as PR authorship"],
              ["**Low**", "Inferred from a weak signal — treat as directional"],
            ],
          ),
          p(
            "A feature's overall confidence is the *lowest* of its parts, not an average. A feature with precise inference cost and a rough build-cost estimate is only as trustworthy as the rough half, and says so.",
          ),
        ],
      },
    ],
  },

  {
    slug: "cost-sources",
    title: "Cost sources",
    blurb: "Connecting providers, and telling Meter what each key is for.",
    topics: [
      {
        slug: "connecting",
        title: "Connecting a provider",
        summary: "Read-only, your own admin credentials, encrypted at rest.",
        blocks: [
          p(
            "On [Cost sources](/cost-sources), connect the providers you are billed by. Meter reads each provider's cost API — it never sends prompts, never makes model calls on your behalf, and never writes anything to your account.",
          ),
          p(
            "Credentials are encrypted before they are stored and are never returned by any API or shown in the UI again. Every table is isolated per tenant at the database level.",
          ),
          p(
            "The first sync backfills twelve months. After that, the refresh control on the Overview pulls the current month so today's spend is current.",
          ),
          note(
            "The current month is always **month-to-date**. Some providers report the most recent days as estimates until the invoice settles; where they do, Meter labels that portion rather than presenting it as billed.",
          ),
        ],
      },
      {
        slug: "classification",
        title: "Classifying workspaces and API keys",
        summary: "Tell Meter which keys are production so the split means something.",
        blocks: [
          p(
            "Providers report spend per workspace and API key. Meter cannot know which of those is production and which is a developer's test key — only you can, so classification is a decision you make, never a guess from a naming convention.",
          ),
          p("Open a connected provider on [Cost sources](/cost-sources) and set each resource to:"),
          list(
            "**Production** — real customer traffic",
            "**Development / Test** — internal experimentation",
            "**Internal** — internal tools and staff usage",
            "**Ignore** — excluded from reporting and optimization totals entirely",
            "**Unclassified** — the default, until you decide",
          ),
          p(
            "This drives the production/development split in the trend charts and the non-production findings in the Copilot. Until keys are classified, that split reads as unclassified — which is why the Overview's Key insights will tell you so.",
          ),
          note(
            "Changing a classification restamps the existing cost rows, so history becomes consistent rather than only applying going forward.",
          ),
        ],
      },
      {
        slug: "self-hosted",
        title: "Self-hosted and open-source models",
        summary: "Models on your own GPUs have infrastructure cost, not per-token cost.",
        blocks: [
          p(
            "A model you run yourself has no per-token price. Its cost is the compute pool it runs on — a monthly infrastructure bill.",
          ),
          p(
            "Meter records usage per feature against the pool, then allocates the pool's bill across features in proportion to that usage. The result is labelled as an allocation with medium confidence, because it is a fair split of a real bill rather than a measured per-call price.",
          ),
        ],
      },
    ],
  },

  {
    slug: "features",
    title: "Features & discovery",
    blurb: "How Meter works out what your product is made of.",
    topics: [
      {
        slug: "discovery",
        title: "How discovery works",
        summary: "Merged pull requests, clustered into proposed features you then curate.",
        blocks: [
          p(
            "Meter reads your **merged pull requests** — title, branch, labels, description, author, and size. It never reads source code.",
          ),
          p(
            "Those pull requests are clustered into proposed features. Each proposal carries the pull requests behind it, so you can always see why Meter thinks a feature exists.",
          ),
          p(
            "Discovery is **manual**: it runs when you run it. Nothing rediscovers your features on a schedule behind your back.",
          ),
          note(
            "Re-running discovery reuses existing features where it can, rather than deleting and recreating them, so build cost stays attributed and your renames survive.",
          ),
        ],
      },
      {
        slug: "curating",
        title: "Reviewing and curating features",
        summary: "Rename, split, merge, delete — the list should match how you think.",
        blocks: [
          p(
            "Discovery proposes; you decide. On [Features](/features) you can rename a proposal, split one that is really two, merge two that are really one, delete a spurious one, or add a feature by hand.",
          ),
          p(
            "Proposals arrive with a confidence badge. Anything Meter could not cluster confidently lands under **Needs review** so it is visible rather than silently misfiled.",
          ),
          p(
            "Confirming a feature does not lock it. You can keep editing at any time, and your edits survive the next discovery run.",
          ),
        ],
      },
      {
        slug: "categories",
        title: "Feature types",
        summary: "Which part of the product a feature belongs to.",
        blocks: [
          p(
            "Each feature carries a type, shown in the Type column on the Overview's By Feature tab: **Chat**, **API**, **UI**, **Docs**, **Data/ETL**, **Auth**, **Reporting**, **Integration** or **Infra**.",
          ),
          p(
            "Discovery guesses it from the vocabulary in the feature's pull requests, and deliberately guesses conservatively: when the evidence does not clearly say, the feature reads **Untagged** rather than being assigned a category nobody chose.",
          ),
          p(
            "You can set it by hand on a feature's detail page, or on the Features review list. A tag you set is never overwritten by a later discovery run.",
          ),
        ],
      },
      {
        slug: "byok",
        title: "Using your own LLM key (BYOK)",
        summary: "Point discovery at your own provider instead of Meter's.",
        blocks: [
          p(
            "Discovery uses an LLM to cluster pull requests. By default that runs on Meter's own model at no cost to you.",
          ),
          p(
            "If you would rather that traffic and spend sat on your own account, configure your own OpenAI-compatible endpoint under **Feature discovery model** in [Settings](/settings): provider, base URL, model and API key.",
          ),
          steps(
            "Choose a provider — the base URL is prefilled and remains editable, so a private deployment or a changed path is fine.",
            "Enter the model you want to cluster with.",
            "Paste your API key and use **Test connection** to check the endpoint, key and model together before saving.",
            "Save. Discovery uses your model from the next run onwards.",
          ),
          p(
            "You can switch back to Meter's model at any time without discarding the configuration, or remove it outright, which deletes the stored key.",
          ),
          note(
            "The key is encrypted at rest and never returned by any API, shown in the UI, or written to logs — including inside provider error messages, which are scrubbed before you see them. Because it is never shown, editing the model or endpoint does not require re-entering it.",
          ),
        ],
      },
    ],
  },

  {
    slug: "sdk",
    title: "The metering SDK",
    blurb: "Optional per-call precision, for when connectors are not enough.",
    topics: [
      {
        slug: "why",
        title: "What the SDK adds",
        summary: "Which feature and which customer made each call — a bill cannot tell you that.",
        blocks: [
          p(
            "Provider cost APIs tell you what was spent. They cannot tell you what it was spent **on**. The SDK closes that gap by reporting, per model call, which feature made it and optionally which of your customers it was for.",
          ),
          p(
            "Without it you still get per-provider, per-key and per-model cost, plus build cost per feature. With it you additionally get:",
          ),
          list(
            "Per-feature inference cost at **high** confidence rather than inferred",
            "Cost per customer (the [By Customer](/) tab)",
            "Latency per feature",
            "Measured optimization findings — duplicate calls and uncached repeated prompt prefixes",
          ),
          note(
            "It is entirely optional. Meter is designed to be useful without it, and installing it later does not invalidate anything you already have.",
          ),
        ],
      },
      {
        slug: "installing",
        title: "Installing it",
        summary: "Wrap your client once; no per-call code.",
        blocks: [
          p(
            "Full copy-paste instructions with your own ingest token live on [Install SDK](/install-sdk) — including a prompt you can hand to a coding agent, which carries your real feature ids and does the wiring for you. The short version:",
          ),
          code(`# Python
from anthropic import Anthropic
from costlyinfra_meter import wrap

client = wrap(Anthropic(), feature_id="<feature-id>")
resp = client.messages.create(...)   # metered automatically`),
          code(`// Node
import { wrap } from "costlyinfra-meter";

const client = wrap(openai, { featureId: "<feature-id>" });
await client.chat.completions.create({ ... });   // metered automatically`),
          p(
            "Configuration is two environment variables — the ingest URL and your token. With neither set, every call is a no-op, so the same code runs in environments where you have not enabled it.",
          ),
          note(
            'Install into the environment your app actually runs in. On Python that means the virtualenv — `python3 -m pip install "costlyinfra-meter>=0.4"`, or a line in your requirements file. The Node package is ESM only, so `import` it rather than `require()` it.',
          ),
        ],
      },
      {
        slug: "what-it-sends",
        title: "What it sends, and what it never sends",
        summary: "Token counts and a feature id. Never prompts, never responses.",
        blocks: [
          p("Each metered call reports:"),
          list(
            "provider, model, input and output token counts",
            "the feature id you configured",
            "how long the call took",
            "optionally, a customer identifier you supply in metadata",
          ),
          p(
            "It never sends prompt text, response text, or source code. Cost is computed **server-side** from Meter's pricing tables, so the SDK never sees prices either.",
          ),
          p(
            "Optimize mode, which is off by default, adds salted hashes and counts describing the *shape* of your traffic — enough to spot a repeated prompt prefix or a duplicated call, never enough to reconstruct one.",
          ),
        ],
      },
      {
        slug: "reliability",
        title: "How it behaves in your application",
        summary: "Queued, batched, bounded, and incapable of breaking your request path.",
        blocks: [
          p(
            "Recording appends to an in-memory queue and returns. A single background worker batches events and posts them — nothing on your request path blocks, throws, or touches the network.",
          ),
          list(
            "**Bounded.** One worker whatever your traffic, and a capped queue. If the queue fills, the oldest events are dropped and counted, rather than growing without limit.",
            "**Batched.** Up to 50 events per request, flushed when a batch fills or after five seconds.",
            "**Retried safely.** A failed batch is retried with backoff. Every attempt carries the same batch id, which the server applies once and then recognises, so a retry can never double-count a feature's cost.",
            "**Fail-safe.** Errors are swallowed. If Meter is down or misconfigured, your application is unaffected.",
          ),
          note(
            "In a short-lived process — a script, or a serverless handler that freezes between invocations — call `meter.flush()` before exiting so queued events are delivered.",
          ),
        ],
      },
    ],
  },

  {
    slug: "dashboards",
    title: "Reading the dashboards",
    blurb: "What each view answers, and when to reach for it.",
    topics: [
      {
        slug: "by-feature",
        title: "By Feature",
        summary: "The money screen: what each feature costs to build and to run.",
        blocks: [
          p(
            "One row per feature, with build cost and inference cost side by side, plus active users, cost per user, request volume and confidence. Click any feature for its full drill-down and evidence trail.",
          ),
          p(
            'The **Worth it?** column is deliberately directional, not a return-on-investment calculation. It compares inference cost against active users; it does not know your revenue. Treat it as "look here", not "cut this".',
          ),
        ],
      },
      {
        slug: "by-provider",
        title: "By Provider",
        summary: "Where the spend goes: provider, model, token type, workspace and key.",
        blocks: [
          p(
            "Inference and build cost live on separate sub-tabs here, because they never blend. The inference view breaks down four ways:",
          ),
          list(
            "**By provider**, each expanding into its models",
            "**By token type** — input, output, cache reads and cache writes",
            "**By workspace and API key**, with the token count beside each amount",
            "**By customer**, when the SDK has tagged calls with one",
          ),
          note(
            "Token *counts* are reported by the provider and are exact. The dollar split **by token type** is derived: providers bill per line item, not per token type, so Meter weights each type by its published rate and apportions the real bill. The parts always sum back to what you were charged, and the view says so.",
          ),
        ],
      },
      {
        slug: "by-developer",
        title: "By Developer",
        summary: "Who spent what on AI tooling, and what shipped.",
        blocks: [
          p(
            "Build cost is the only cost attributable to a person, so this view is build-only. It shows spend per developer, broken down by the tool they used.",
          ),
          p(
            "Below it, **Engineering activity** shows what each developer shipped over the same period — pull requests, features touched, commits, files, and lines added and removed — alongside their tooling spend and cost per PR.",
          ),
          note(
            "That table counts **activity, not performance**. It measures what was shipped, not how hard or how valuable it was, and a large pull request is not a better one. Whoever merges the most pull requests is often not whoever writes the most code. Read it next to the spend, never as a ranking of people.",
          ),
        ],
      },
      {
        slug: "by-customer",
        title: "By Customer",
        summary: "Which of your customers your AI spend is going on.",
        blocks: [
          p(
            "The one breakdown a provider bill cannot produce: a bill records what was spent, never on whose behalf. It is populated only from SDK-metered calls tagged with `metadata.customer_id`.",
          ),
          p(
            "Each customer shows spend, share, request volume, **cost per request** and change against the prior period. Cost per request is often the interesting column — a customer can make a fraction of the calls and cost several times more.",
          ),
          note(
            "Metered spend is a subset of your bill, not a second version of it. The view states what share of the real inference bill carries a customer tag, so the part is never mistaken for the whole.",
          ),
        ],
      },
      {
        slug: "insights-and-trends",
        title: "Key insights and trends",
        summary: "Generated observations, and how to read the charts.",
        blocks: [
          p(
            "**Key insights** are generated deterministically from your own numbers — no model, no black box — and ranked so anomalies and cost-cutting angles come first. Each names the figure behind it. They only appear when they clear both a percentage and a dollar threshold, so a small tenant is not shown five breathless bullets about $30.",
          ),
          p("Charts follow two rules worth knowing:"),
          list(
            "A **partial month is never compared to a full one**. For the current month you get a projected pace, labelled as a projection, rather than a misleading month-over-month figure.",
            "The trend line is smoothed, but it **cannot draw spend that did not happen** — the curve never rises above a peak or dips below a floor in your data.",
          ),
        ],
      },
    ],
  },

  {
    slug: "optimize",
    title: "Optimization Copilot",
    blurb: "Where the money is going that it does not need to.",
    topics: [
      {
        slug: "how-it-works",
        title: "How findings are produced",
        summary: "Two tiers of evidence, never mixed up with each other.",
        blocks: [
          p(
            "[Optimize](/optimize) produces findings from two different kinds of evidence, and is explicit about which is which.",
          ),
          list(
            "**Measured** findings come from SDK telemetry: duplicated calls, repeated prompt prefixes that are not being cached, a model larger than the traffic needs. These carry a quantified saving because the traffic was observed.",
            "**Billing-data** findings come from your bills alone and need no SDK: unclassified spend, unattributed spend, non-production keys, a single key dominating the bill, sharp growth, no cost controls in place.",
          ),
          p(
            "Billing findings never claim a saving. They report **spend under review** — money worth a decision — because a bill alone cannot tell you whether a development key is still needed.",
          ),
        ],
      },
      {
        slug: "savings-language",
        title: "What the savings numbers mean",
        summary: "Observed spend is not saved money, and Meter will not call it that.",
        blocks: [
          table(
            ["Label", "Meaning"],
            [
              ["**Measured**", "A before/after reduction actually observed in your traffic"],
              [
                "**Modeled ceiling**",
                "An upper bound from your measured token mix — quality-gated, realize with care",
              ],
              [
                "**Not quantified**",
                "Real spend worth reviewing, with no claim about what you would save",
              ],
            ],
          ),
          p(
            'The distinction is deliberate. A finding that says "$1,800 of spend sits on one unclassified key" is telling you where to look. It is not telling you that $1,800 is recoverable, and it will not pretend otherwise.',
          ),
        ],
      },
      {
        slug: "applying",
        title: "Applying and verifying a change",
        summary: "Projected, then realized, then verified against your actual bill.",
        blocks: [
          p(
            "When you act on a measured finding, mark it applied. Meter then tracks it through three states:",
          ),
          steps(
            "**Projected** — what the finding estimated before you changed anything.",
            "**Realized** — the change measured against the following period's actual spend.",
            "**Verified** — the reduction held across more than one period, so it was not a quiet month.",
          ),
          p(
            "This is the part most cost tools skip. An estimate that is never checked against the invoice is a guess with a dollar sign on it.",
          ),
        ],
      },
    ],
  },

  {
    slug: "reconciliation",
    title: "Invoice reconciliation",
    blurb: "Check Meter's numbers against the provider's own bill, and see what differs.",
    topics: [
      {
        slug: "what-it-is",
        title: "What invoice reconciliation does",
        summary: "Compares a provider billing export against tracked spend, and explains the gap.",
        blocks: [
          p(
            "Import the CSV your provider bills you with, and Meter lines it up against the spend it already tracked for the same period — then tells you what accounts for the difference, line by line, with the evidence for each explanation.",
          ),
          p(
            "It answers the question a finance team asks first: *does this tool agree with my actual invoice, and if not, why not?*",
          ),
          note(
            "This is a different thing from [Reconciliation: why the bill is always right](/help/concepts/reconciliation), which is internal and automatic — that one keeps SDK metering honest against the provider's cost API. This one compares Meter against the **invoice you were sent**, and only happens when you import one.",
          ),
          p(
            "It is **opt-in and additive**. Until you turn it on there is no menu item, and turning it on changes nothing about your existing cost data, dashboards, Optimize or Alerts.",
          ),
        ],
      },
      {
        slug: "turning-it-on",
        title: "Turning it on",
        summary: "Off by default, per organization, and safe to turn off again.",
        blocks: [
          steps(
            "Open [Reconciliation](/reconciliation) — the page is reachable whether or not the feature is on.",
            "Press **Enable reconciliation**. That adds a Reconciliation entry under Analyze in the sidebar.",
            "Import a statement, and reconcile it.",
          ),
          p(
            "Turning it off again hides the section and stops all of its work. It deletes nothing: your imports, runs and explanations are all still there if you turn it back on.",
          ),
          note(
            "An operator can disable the module for a whole installation with `METER_RECONCILIATION=off`, regardless of what any organization has chosen. That is a kill switch, not a data change.",
          ),
        ],
      },
      {
        slug: "importing",
        title: "Importing a statement",
        summary: "Map your columns, see what would be imported, then commit.",
        blocks: [
          p(
            "Providers do not agree on a billing CSV format, so Meter does not pretend to know yours. It reads your header row, guesses which column holds each field, and shows you the guess to correct before anything is stored.",
          ),
          p("Only two fields are required:"),
          list(
            "**a date** — when the usage happened",
            "**a usage amount** — the charge before tax, credits and fees",
          ),
          p(
            "Everything else — model, workspace, API key, category, tax, credits, fees, invoice and line ids — improves the match if your export has it, and is simply absent if it does not.",
          ),
          p(
            "Before you commit, the preview shows the period covered, the currency, the financial totals, how many rows are readable, and every row that is not — with the reason. Rows that cannot be read are left out of the comparison; the rest still import.",
          ),
          note(
            "Only the columns you map are stored. A billing export that also carries a contact name or an account manager's email keeps those to itself.",
          ),
        ],
      },
      {
        slug: "how-the-comparison-works",
        title: "How the comparison works",
        summary: "Usage against usage, on the most specific dimensions both sides share.",
        blocks: [
          p(
            "The headline number is the **provider's usage subtotal** against **Meter's tracked usage cost**. Tax, credits, discounts and fees are read from the statement and reported separately — never added to either side.",
          ),
          p(
            "That distinction matters more than it sounds. Comparing a tax-inclusive invoice total against usage cost would report a discrepancy every month there is any tax at all, and call it missing usage. Meter does not do that.",
          ),
          p(
            "Each statement line is matched against tracked spend using the most specific dimensions both sides carry, in this order:",
          ),
          table(
            ["Strategy", "Matches on"],
            [
              ["Line item id", "The provider's own id for the line, where the export has one"],
              ["Account + key + date + model", "Workspace, API key, service date and model"],
              ["Account + date + model", "Workspace, service date and model"],
              ["Aggregate", "Workspace and billing period only"],
            ],
          ),
          p(
            "Every comparison records which strategy produced it, and an aggregate comparison says so. A tracked row is never counted against two statement lines, and totals are never adjusted to agree.",
          ),
          note(
            "Only connector spend is compared. SDK-metered spend is a second observation of the same calls, and self-hosted spend is not on a provider invoice at all — counting either would double the tracked side against your bill.",
          ),
        ],
      },
      {
        slug: "statuses-and-tolerance",
        title: "Statuses and tolerance",
        summary: "Two tolerances, both applied, and both stored with the run that used them.",
        blocks: [
          p("Every reconciliation ends in one of these:"),
          table(
            ["Status", "Means"],
            [
              ["**Matched**", "The two usage figures agree exactly"],
              ["**Within tolerance**", "They differ by less than you have said you care about"],
              ["**Discrepancy**", "A material difference worth looking at"],
              [
                "**Incomplete data**",
                "Something is missing or not comparable — a currency mismatch, or no tracked data for the period",
              ],
              ["**Failed**", "The calculation could not run; the reason is on the run"],
            ],
          ),
          p(
            "Two tolerances apply, and a difference inside **either** one is forgiven: an absolute amount (default $1.00) and a percentage (default 0.5%). The absolute bound covers rounding on a small bill; the percentage covers proportional drift on a large one.",
          ),
          note(
            "The tolerances in force are copied onto every run. Changing them later never rewrites what a past run was judged against.",
          ),
        ],
      },
      {
        slug: "reading-a-discrepancy",
        title: "Reading a discrepancy",
        summary: "Every explanation says how sure it is, and shows what it is based on.",
        blocks: [
          p(
            "A difference is classified when the data supports a classification: usage the provider billed that Meter has no record of, usage Meter tracked that the statement never mentions, a price difference, a line dated outside the period, a workspace that is not connected, a duplicated statement row, an unrecognised model, or simply an unexplained difference.",
          ),
          p("Each explanation carries a confidence, and the distinction is real:"),
          table(
            ["Confidence", "Means"],
            [
              [
                "**Confirmed**",
                "It follows directly from the data — a line dated outside the period, or a repeated line id",
              ],
              ["**Possible**", "It is the most likely reading of the evidence, not a fact"],
              ["**Unknown**", "The data does not say"],
            ],
          ),
          p(
            "The evidence behind each explanation is shown with it, so you can disagree. Meter will not tell you a cause is confirmed because it is plausible.",
          ),
        ],
      },
      {
        slug: "recalculating-and-audit",
        title: "Recalculating, history and export",
        summary: "Runs are immutable; a recalculation is a new one beside the old.",
        blocks: [
          p(
            "Recalculate after a corrected export, after ingestion catches up, or after changing your tolerances. It writes a **new** run and leaves the previous one exactly as it was, so you can always see what you knew and when.",
          ),
          list(
            "**Import history** shows every file, who imported it, when, and how many runs came from it.",
            "**Removing an import** is recoverable — it stops being used for new calculations, and its rows and past runs stay readable.",
            "**Export report** downloads the whole comparison as CSV: the totals, each financial category, every match with its strategy, classification, confidence and evidence, and the tolerances used.",
          ),
          note(
            "Every material action — import, replace, remove, reconcile, export — is recorded with who did it. The audit trail records what happened, never the contents of the file.",
          ),
        ],
      },
      {
        slug: "what-it-never-does",
        title: "What it never does",
        summary: "It reports differences. It does not resolve them for you.",
        blocks: [
          list(
            "**It never changes your cost data.** A statement is evidence, not a correction. No tracked row is updated, reclassified or deleted, and no total anywhere else in Meter moves because you imported a bill.",
            "**It never converts currencies.** A statement in one currency and tracked data in another is reported as incomplete data, with both figures intact.",
            "**It never manufactures a match.** If a line cannot be matched, it is shown as unmatched rather than absorbed into an aggregate that happens to balance.",
            "**It never treats tax, credits or fees as usage.** They are on your invoice and they are reported, but they are not inference cost.",
          ),
          p(
            "If reconciliation is switched off, or fails, nothing else changes: ingestion, the [Overview](/), [Optimize](/optimize) and [Alerts](/alerts) do not know it exists.",
          ),
        ],
      },
    ],
  },

  {
    slug: "alerts",
    title: "Alerts",
    blurb: "Getting told when spend does something you should know about.",
    topics: [
      {
        slug: "creating",
        title: "Creating an alert",
        summary: "A metric, a scope, a condition and a window.",
        blocks: [
          p("An alert is four choices, made on [Alerts](/alerts):"),
          table(
            ["Choice", "Options"],
            [
              [
                "**Metric**",
                "A monthly spend figure — inference cost, build cost, combined AI cost, cost per active user, token usage, unattributed cost — or one of the run-level metrics below",
              ],
              [
                "**Scope**",
                "The whole organization, a feature, an AI application, or (for inference metrics) a provider or model",
              ],
              [
                "**Condition**",
                "Exceeds a threshold, falls below one, increases by a percentage, or reaches a percentage of a budget",
              ],
              ["**Window**", "Hourly, daily, weekly or monthly"],
            ],
          ),
          p(
            "A cooldown stops one ongoing problem from paging you repeatedly, and recovery notifications tell you when it clears.",
          ),
        ],
      },
      {
        slug: "run-level",
        title: "Alerting on agent runs",
        summary: "Stuck runs, retry loops, cost per run and cache efficiency.",
        blocks: [
          p(
            "The metrics above answer *how much did we spend this month*. These answer *what is happening to our runs right now*, and are measured from the [traces](/traces) your SDK reports — never from an estimate.",
          ),
          table(
            ["Metric", "What it measures"],
            [
              [
                "**Stale agent runs**",
                "Runs still marked running that have not reported anything for longer than your staleness threshold. Set the threshold to 0 to hear about the first one.",
              ],
              [
                "**Longest agent runtime**",
                "The longest run in the window, in minutes. A run still in flight counts at its runtime so far — which is the only way an unfinished run can be caught.",
              ],
              [
                "**Steps in a single run**",
                "The worst single run's step count, not the average, so one runaway workflow is not hidden by quiet ones beside it.",
              ],
              [
                "**Repeats of one step in a run**",
                "How many times any one step ran inside a single run. A workflow that calls the same tool twice by design reads as 2 forever; one stuck in a retry loop climbs.",
              ],
              [
                "**Cost per agent run**",
                "Average cost of the runs that finished in the window. Runs still in flight are excluded, so a busy period is not made to look cheap by partial costs. This is the one run-level metric that can also alert on a percentage increase — which is how you catch a release that made every run dearer.",
              ],
              [
                "**Cost incurred by failed runs**",
                'Money spent on runs that ended in error. Zero is a real, healthy reading, so a threshold of 0 means "tell me about any of it".',
              ],
              [
                "**Prompt cache hit rate**",
                "The share of input tokens served from the prompt cache. This is the one metric where the alarm is a *fall*: efficiency dropping is what costs money.",
              ],
            ],
          ),
          note(
            "Scope a run-level alert to an **AI application** to alert per product surface. Provider and model scopes are not offered here, because a single run can call several providers and belongs wholly to none of them.",
          ),
          p(
            "These reuse everything else about alerts — the same channels, cooldowns, incidents and recovery notifications.",
          ),
        ],
      },
      {
        slug: "delivery",
        title: "Delivery and channels",
        summary: "In-app, email, Slack or webhook — and what happens when delivery fails.",
        blocks: [
          p(
            "Alerts can notify in-app, by email, to a Slack webhook, or to a webhook of your own. An alert can use several at once.",
          ),
          p(
            "Delivery failures are visible rather than silent: an alert whose channel is failing shows a delivery-error status, so a misconfigured webhook does not quietly mean no alerts.",
          ),
          note(
            "Use **Send test** when creating an alert to confirm the channel works before you rely on it.",
          ),
        ],
      },
    ],
  },

  {
    slug: "trust",
    title: "Privacy & trust",
    blurb: "What Meter reads, stores, and can never do.",
    topics: [
      {
        slug: "what-we-read",
        title: "What Meter reads",
        summary: "Read-only, metadata only, your own credentials.",
        blocks: [
          list(
            "**All connectors are read-only.** Meter never writes to your provider accounts or your repositories.",
            "**From GitHub**: pull request metadata — title, branch, labels, description, author, size. Never source code.",
            "**From providers**: cost and usage reports. Never prompts or responses.",
            "**From the SDK**: token counts, model, feature id, latency. Never prompt or response content.",
          ),
          p(
            "Credentials are encrypted before storage using your own deployment secret, and are never returned by an API or shown again in the UI.",
          ),
        ],
      },
      {
        slug: "isolation",
        title: "Tenant isolation",
        summary: "Enforced by the database, not by application code remembering to filter.",
        blocks: [
          p(
            "Every tenant-scoped table enforces row-level security in Postgres, and the application connects through a role that cannot bypass it. A query that forgets to filter by tenant returns nothing rather than someone else's data.",
          ),
          p(
            "That includes credentials, cost rows, features, alerts and your discovery LLM configuration.",
          ),
        ],
      },
      {
        slug: "settings",
        title: "Organization settings",
        summary: "Name, time zone, currency, and privacy preferences.",
        blocks: [
          p(
            "[Settings](/settings) holds your organization profile and privacy preferences: display name, time zone, reporting currency, how customer identifiers are stored, whether prompt content may ever be stored, and your data retention window.",
          ),
          note(
            "Meter stores no prompt text today. The **store prompt content** preference is a forward-looking guarantee rather than a switch over existing data.",
          ),
        ],
      },
    ],
  },

  {
    slug: "troubleshooting",
    title: "Troubleshooting",
    blurb: "The questions that come up most, and what to check.",
    topics: [
      {
        slug: "numbers-dont-match",
        title: "My numbers do not match my provider's console",
        summary: "Usually period, classification, or estimated-but-not-yet-billed spend.",
        blocks: [
          p("Work through these in order:"),
          steps(
            "**Check the period.** Meter's *This month* is the calendar month to date. Provider consoles often default to a rolling 30 days or to a billing cycle that does not start on the 1st.",
            "**Check for ignored resources.** Any workspace or key classified as **Ignore** is excluded from Meter's totals by design.",
            "**Check for estimated spend.** Recent days may be estimated until the provider settles them; Meter labels that portion.",
            "**Re-sync.** Use the refresh control on the [Overview](/) to pull the current month again.",
          ),
          p(
            "If a gap persists after that, it is worth reporting — a real discrepancy is a bug, not a rounding difference.",
          ),
        ],
      },
      {
        slug: "no-features",
        title: "Discovery found no features",
        summary: "Almost always repository scope or the analysis window.",
        blocks: [
          list(
            "**No merged pull requests in the window.** Discovery reads *merged* PRs from the last 90 days by default. A repository that only takes direct commits to the main branch gives it nothing to read.",
            "**The wrong repositories are selected.** Check the repository scope on [Features](/features).",
            "**The token cannot see them.** Private repositories need a token with access.",
          ),
          note(
            "If clustering cannot form confident groups it falls back to a heuristic rather than producing nothing, so an empty result usually means no input, not a failure.",
          ),
        ],
      },
      {
        slug: "sdk-not-reporting",
        title: "The SDK is installed but nothing appears",
        summary: "Configuration, feature id, batching delay, or a short-lived process.",
        blocks: [
          steps(
            "**Check both environment variables** are set. With either missing the SDK is deliberately a silent no-op.",
            "**Wait a few seconds.** Events are batched and flushed within about five seconds — they are not instant by design.",
            "**Check the feature id** matches a feature that exists. An unknown id is not dropped; its spend lands in Unattributed.",
            "**In a short-lived process**, call `meter.flush()` before exiting, or the process may end before the worker runs.",
            "**Check `meter.dropped`.** If it is climbing, the endpoint is unreachable or rejecting the requests.",
          ),
          p(
            "A wrong token produces no error in your application — that is intentional, since metering must never break your request path — so the token is worth checking explicitly.",
          ),
        ],
      },
      {
        slug: "everything-unattributed",
        title: "Most of my spend shows as Unattributed",
        summary: "Expected before the SDK is installed; here is what closes the gap.",
        blocks: [
          p(
            "With only cost-API connectors, Meter knows what each provider and key cost but not which feature made each call. That is honest rather than broken — the spend is real and shown, just not yet attributed.",
          ),
          p("Two things reduce it:"),
          list(
            "**Install the [metering SDK](/install-sdk)** so calls carry a feature id. This is the big one.",
            "**Classify your keys** on [Cost sources](/cost-sources) so spend at least separates production from development.",
          ),
        ],
      },
      {
        slug: "getting-help",
        title: "Getting help",
        summary: "The assistant answers from this handbook; a human answers the rest.",
        blocks: [
          p(
            "The chat button in the bottom-right corner opens the Meter assistant. It answers from **this handbook and nothing else**, and every reply links to the topics it drew on, so you can check the answer against the source rather than taking its word for it.",
          ),
          p("That boundary is deliberate. It means the assistant can tell you:"),
          list(
            "how a number is worked out, and where it comes from",
            "what a screen, a column or a setting means",
            "how to connect a provider, install the SDK, or fix a common problem",
          ),
          p("And it means the assistant will not:"),
          list(
            "read your own data — it cannot see your spend, features or invoices, so it can never tell you *your* numbers",
            "guess at anything the handbook does not cover; it says so instead",
          ),
          note(
            "When the handbook has no answer, the assistant offers **Contact support** — that goes to a person, and is the right route for anything about your account, your bill, or a number you believe is wrong.",
          ),
        ],
      },
    ],
  },
];

/** Flattened, for search and prev/next navigation. */
export const ALL_TOPICS: { category: Category; topic: Topic }[] = CATEGORIES.flatMap((category) =>
  category.topics.map((topic) => ({ category, topic })),
);

export function findTopic(categorySlug: string, topicSlug: string) {
  return ALL_TOPICS.find(
    ({ category, topic }) => category.slug === categorySlug && topic.slug === topicSlug,
  );
}
