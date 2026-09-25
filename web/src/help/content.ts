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
import { MIN_SDK } from "../pages/installPrompt";

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
            "**Connect a provider** on [Connect sources](/cost-sources) — Anthropic, OpenAI, or whichever you use. Meter reads its cost API. Read-only, using your own admin credentials, stored encrypted.",
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
      {
        slug: "ask-meter",
        title: "Ask Meter",
        summary: "The assistant answers from your own data and this handbook, and nothing else.",
        blocks: [
          p(
            "The assistant is not a general chatbot. It answers from two sources: this handbook, for how Meter works, and a read-only snapshot of your own account, for what is actually happening in it.",
          ),
          p(
            "Numbers in an answer come from that snapshot, never from the model's memory. A plausible invented figure is the worst thing an assistant on a cost tool could produce.",
          ),
          note(
            "The snapshot carries nothing that is not already on a screen — no prompt or response text, no customer identifiers, no credentials — and it is read under the same tenant isolation as every other query.",
          ),
          p(
            "To ask the same kinds of question from your editor instead, see [Coding agents](/help/trust/coding-agents).",
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
    title: "Connect sources",
    blurb: "Connecting providers, and telling Meter what each key is for.",
    topics: [
      {
        slug: "connecting",
        title: "Connecting a provider",
        summary: "Read-only, your own admin credentials, encrypted at rest.",
        blocks: [
          p(
            "On [Connect sources](/cost-sources), connect the providers you are billed by. Meter reads each provider's cost API — it never sends prompts, never makes model calls on your behalf, and never writes anything to your account.",
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
          p(
            "Open a connected provider on [Connect sources](/cost-sources) and set each resource to:",
          ),
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
      {
        slug: "infrastructure",
        title: "Infrastructure (cloud) cost",
        summary: "The cloud your product runs on, read in full from AWS, Azure, GCP or Vercel.",
        blocks: [
          p(
            "Model bills and coding-tool spend do not cover the cloud a product actually runs on. Connect a cloud account and Meter reads its cost API in full, keeping every line item's own service name and dimensions verbatim.",
          ),
          p(
            "**Nothing is dropped.** A service Meter has never heard of is still infrastructure, stored with its raw name so it can be reclassified later without re-fetching the bill.",
          ),
          p(
            "**Nothing is counted twice.** Amazon Bedrock spend already arrives through the Bedrock inference connector, which stays its authoritative path. Bedrock line items are recorded here but not counted, and every total filters on that.",
          ),
          p(
            "A bill exported in the FinOps **FOCUS** format is read against that specification rather than by guessing at column names — see [Importing a FOCUS export](/help/cost-sources/focus).",
          ),
          note(
            "Infrastructure cost currently lives on its own tab and is **not yet folded into feature cost or the Overview's totals**. Where a feature is assigned, it comes from a cost-allocation tag you activated or a rule you wrote — a service name on its own never implies a feature.",
          ),
        ],
      },
      {
        slug: "focus",
        title: "Importing a FOCUS export",
        summary:
          "Upload a FinOps FOCUS billing file from any cloud, read against the published spec.",
        blocks: [
          p(
            "FOCUS is the FinOps Foundation's open specification for billing data. A provider that publishes a FOCUS export emits the same columns as every other one, so a file from AWS, Azure, GCP, OCI or a FinOps pipeline covering several of them at once is read the same way.",
          ),
          p(
            "Upload one under **FOCUS export** on [Connect sources](/cost-sources). A file dropped on any other import card is recognised as FOCUS too when it turns out to be one — the format is detected, not declared.",
          ),
          p(
            "Every other bill import matches columns by what they appear to mean, because a vendor's own column names cannot be verified. FOCUS names are published, so they are matched exactly and read for what the specification says they mean:",
          ),
          list(
            "**Which dollars.** A FOCUS row carries up to four costs. `BilledCost` is what the invoice says and is what Meter reads by default; `EffectiveCost` amortizes a commitment purchase across the periods it covers. You can switch before importing, and the column used is stored with every row — so choosing differently next month does not silently reinterpret what is already there.",
            "**What kind of charge.** `ChargeCategory` separates Usage and Purchase from Tax, Credit and Adjustment. All of them are imported, because leaving any out would make Meter's total disagree with the invoice it reconciles against — and the preview reports what each category came to, because sales tax is not the cost of running a service.",
            "**Corrections.** A row whose `ChargeClass` is `Correction` restates a billing period that was already closed. It is imported and flagged, so a month whose total moves after somebody wrote it down is explainable.",
            "**Tags.** FOCUS keeps every tag in one JSON column. The key you attribute by is read out of that map, with or without a provider prefix — `feature`, `aws:feature` and `someScheme/feature` all match.",
          ),
          note(
            "Nothing is written until you confirm. The preview names the cost column it read, the charge-category split, any corrections, and any column it did not recognise — a vendor's own `x_` columns are expected rather than reported as junk.",
          ),
        ],
      },
      {
        slug: "build-cost-sources",
        title: "Where build cost comes from",
        summary: "A seat roster, a CSV export, or entered by hand.",
        blocks: [
          p(
            "**Seat sources.** Connect an identity provider and map an app roster to a priced coding tool and plan. A sync prices each seat and feeds the same pull-request allocator discovery uses, so seats land on features without a spreadsheet.",
          ),
          p(
            "**A CSV.** A Cursor-for-Teams seat export, or any CSV with a developer and an amount. One file can backfill several months at once.",
          ),
          p(
            "**By hand.** Spend with no export at all can be entered directly, and a later sync will not erase what you typed.",
          ),
          note(
            "A seat whose identity cannot be matched to a known pull-request author is still counted — it lands in Unattributed rather than being dropped, or guessed onto a feature it may not belong to.",
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
            "Discovery runs when you run it. You can also switch on a **nightly** run from the Features screen — it is off until you do — and choose how far back each one looks.",
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
    slug: "products",
    title: "Products",
    blurb: "Grouping features into the things you actually sell.",
    topics: [
      {
        slug: "what-a-product-is",
        title: "What a product is",
        summary: "A grouping above features, so you can see what each thing you sell costs.",
        blocks: [
          p(
            "A **product** is a thing you sell. One GitHub organization often holds several, and features live inside them: a product's cost is the cost of its features, with build and run kept apart as they are everywhere else.",
          ),
          p(
            "A feature belongs to the product that owns the repositories its pull requests came from. Discovery records those repositories as evidence, so a product badge can always be traced back to the work behind it.",
          ),
          p(
            "Your choice always wins. Assign a feature to a product by hand and no discovery run — and no re-application of the repository mapping — will overwrite it. That is the same rule a feature's Type follows.",
          ),
          note(
            "A feature whose pull requests span repositories in two different products stays **Unassigned** rather than being guessed into one of them. A majority vote over pull-request counts would be a number you could not check.",
          ),
          p(
            "An [application](/help/dashboards/applications) is not a product. An application is an instrumented service, named by your SDK; a product is your own grouping, and it covers build cost too.",
          ),
        ],
      },
      {
        slug: "mapping-repositories",
        title: "Mapping repositories to products",
        summary: "Tick which repositories each product is built in; Meter assigns the features.",
        blocks: [
          steps(
            "Open [Products](/products) and add a product for each thing you sell.",
            "Tick the repositories it is built in. A repository belongs to one product.",
            "Re-apply the mapping. Every feature whose evidence points at those repositories is assigned.",
          ),
          p(
            "Mapping twenty repositories by hand is the slow way, so Meter proposes products from repository names — `sentinel-api`, `sentinel-web` and `sentinel-ingest` become a suggested **Sentinel**. Nothing is created until you accept a suggestion.",
          ),
          p(
            'For a repository that several products genuinely share, make a product for it — "Platform", "Shared" — rather than splitting its cost by a percentage nobody could defend.',
          ),
          note(
            "Features spanning two products are listed on the Products page for you to decide. That list is the only part of the mapping that needs a person.",
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
            "Optimization findings from real traffic — repeated requests, and prompt prefixes being resent instead of cached",
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
from costlyinfra_meter import Meter

meter = Meter()                       # reads the environment variables below
client = meter.wrap(Anthropic(), feature_id="<feature-id>")
resp = client.messages.create(...)   # metered automatically`),
          code(`// Node
import { Meter } from "costlyinfra-meter";

const meter = new Meter();
const client = meter.wrap(openai, { featureId: "<feature-id>" });
await client.chat.completions.create({ ... });   // metered automatically`),
          p(
            "Configuration is two environment variables — the ingest URL and your token. With neither set, every call is a no-op, so the same code runs in environments where you have not enabled it.",
          ),
          note(
            'Install into the environment your app actually runs in. On Python that means the virtualenv — `python3 -m pip install "costlyinfra-meter>=' +
              MIN_SDK +
              '"`, or a line in your requirements file. The Node package is ESM only, so `import` it rather than `require()` it.',
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
            "Metering never sends prompt text, response text, or source code. Cost is computed **server-side** from Meter's pricing tables, so the SDK never sees prices either. The one exception is optional: with capture switched on in the SDK (2.1 or later) **and** your organization's consent for the feature, it sends a small sample of prompt text for [Prompt optimization](/help/trust/prompt-optimization), on its own channel, and never tool calls, tool results, images or files.",
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
      {
        slug: "opentelemetry",
        title: "Using OpenTelemetry instead",
        summary:
          "Already tracing with OpenTelemetry? Point an exporter at Meter; nothing to install.",
        blocks: [
          p(
            "If your application already emits OpenTelemetry traces — from OpenLLMetry, OpenInference, or OpenTelemetry's own GenAI instrumentation — Meter reads them directly. You add Meter as a trace exporter: no SDK, and no change at the call site. Copy-paste configuration with your own endpoint is on [Install SDK](/install-sdk), under **OpenTelemetry**.",
          ),
          p(
            "Spans become the same runs and steps the SDK records, priced with the same rates and reconciled against your provider bill the same way. Meter reads a fixed list of attributes:",
          ),
          list(
            "`service.name` — the application",
            "`gen_ai.system` — the provider",
            "`gen_ai.response.model`, or else `gen_ai.request.model` — the model",
            "`gen_ai.usage.input_tokens` and `output_tokens` — tokens, and from them cost",
            "the span with no parent — the run, with its name, start and end",
            "`meter.feature_id` and `meter.customer_id` — the feature and customer, when you set them",
          ),
          p(
            "Nothing else is read: no other attribute, and no span event. Prompt and response content that instrumentation attaches is discarded on arrival and never stored, and the export response says so. Turn content capture off in your instrumentation anyway — discarded on arrival still means it was sent.",
          ),
          note(
            "OpenTelemetry does not copy a span's attributes onto its children, and the spans for model calls are made by your instrumentation library, not by you. So set `meter.feature_id` for a whole service with `OTEL_RESOURCE_ATTRIBUTES`, or per run with baggage and a `BaggageSpanProcessor`. A feature set only on the run's own span reaches the steps exported alongside it, but not steps exported earlier — and in a long run they usually are.",
          ),
        ],
      },
      {
        slug: "splunk",
        title: "Sending from Splunk Observability Cloud",
        summary: "Add Meter as a second destination in the Splunk Collector you already run.",
        blocks: [
          p(
            "If your traces go to Splunk Observability Cloud, they already pass through the Splunk Distribution of the OpenTelemetry Collector. Meter takes a copy from there: you add Meter as a second exporter, in a pipeline of its own, and what goes to Splunk does not change. Configuration for a Linux host and for Kubernetes is on [Install SDK](/install-sdk), under **Splunk**.",
          ),
          p(
            "Splunk's AI instrumentation follows OpenTelemetry's GenAI conventions, so Meter reads those spans exactly as described in [Using OpenTelemetry instead](/help/sdk/opentelemetry), including how to set `meter.feature_id`.",
          ),
          note(
            "Meter's pipeline deletes prompt, response, tool and exception attributes, and drops span events, before anything leaves your network. You can keep content in Splunk while Meter's copy never carries it.",
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
      {
        slug: "by-product",
        title: "By Product",
        summary: "What each thing you sell cost to build and to run, and how that moved.",
        blocks: [
          p(
            "One row per product, with build cost and inference cost in separate columns, the features it holds and the repositories behind it. Above the table, a stacked bar per month shows one kind of money at a time — the toggle says which, because a segment combining the two would be a blended figure this product never shows.",
          ),
          p(
            "Two rows sit beneath the products and they are not the same thing. **Unassigned** is spend on features that belong to no product yet: map a repository and it moves. **Unattributed** is spend Meter cannot tie to any feature at all.",
          ),
          note(
            "Part of Unattributed is the gap between a provider's bill and what your SDK metered. It has no row to move, so it can never belong to a product however complete your mapping becomes.",
          ),
        ],
      },
      {
        slug: "traces",
        title: "Traces",
        summary: "One agent run, step by step, and what each step cost.",
        blocks: [
          p(
            "A monthly total answers what a feature cost. A **trace** answers why one run cost what it did: the steps it took, in order, each with its model, tokens, latency and price.",
          ),
          p(
            "Traces arrive from the metering SDK or from OpenTelemetry. Each finished model step is priced with the same price book the monthly totals use, so a trace and a total are two views of one number rather than two numbers that can drift.",
          ),
          note(
            "Prompt and response text never arrives. A field that would carry content is refused loudly rather than dropped quietly, so instrumenting content capture tells you it was refused instead of leaving you believing it worked.",
          ),
        ],
      },
      {
        slug: "applications",
        title: "Applications",
        summary: "The instrumented service or agent a run belongs to.",
        blocks: [
          p(
            'An **application** is what your SDK calls itself — "support-agent", "document-review". It appears the first time an instrumented workflow reports a run, named by the slug you set in its environment, so instrumenting a service is one line rather than a setup step.',
          ),
          p(
            "It reads like the feature views one level up: spend, runs, cost per run, error rate, and which features each application touched.",
          ),
          note(
            "An application is not a [product](/help/products/what-a-product-is). It covers only instrumented spend — no build cost, and no provider spend that arrived without the SDK — so the two answer different questions.",
          ),
        ],
      },
      {
        slug: "budget-and-forecast",
        title: "Budget and forecast",
        summary: "Set a budget; Meter projects the open month from the spend already observed.",
        blocks: [
          p(
            "Set a monthly or annual budget in [Settings](/settings). There is no budget until you set one, and Meter never invents a default — a missing budget is a real answer the Overview will show you.",
          ),
          p(
            "The Overview then forecasts the open month from the daily spend so far. A month that is over is never projected: it reports its final figure and says so.",
          ),
          p(
            "A window spanning several months gets its budget prorated by calendar days, and the panel shows how it arrived there. A number nobody can explain is not much use to a CFO.",
          ),
        ],
      },
    ],
  },

  {
    slug: "optimize",
    title: "Optimize",
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
            "**SDK-telemetry** findings come from your own traffic: requests sent more than once, prompt prefixes resent instead of cached, a model larger than the traffic needs. These carry a number because the traffic was observed — but see the table below, because only some of them are savings rather than ceilings.",
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
              [
                "**Measured**",
                "Computed from observed traffic, not a percentage — e.g. prefix tokens the provider itself reported. Priced at list rate, not read off your invoice",
              ],
              [
                "**Modeled ceiling**",
                "An upper bound. The count is real; realizing it depends on something Meter cannot check — that a smaller model holds quality, or that a repeated request could safely have been answered from the first",
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
        slug: "repeated-requests",
        title: "Repeated request candidates",
        summary: "What Meter measures about repeats, and the part it cannot know.",
        blocks: [
          p(
            "With optimize mode on, the SDK notices when your application sends the **same request twice** — the whole request, not just the prompt: model, system instructions, tools, temperature, output limits, response format and the rest. Two calls differing in any of them are two different calls.",
          ),
          p("What that finding does and does not claim:"),
          list(
            "**The count is exact.** Those requests really were identical, within one application, feature, operation, environment and customer scope.",
            "**Whether the repeat was avoidable is not measured.** Serving the second from the first assumes the answer was still fresh, the caller was entitled to the same data, the repeat was not deliberate sampling or a retry, and nothing outside the prompt had changed. Meter sees none of that, so it reports a **ceiling**, never a saving.",
            "**The dollars are list price.** Meter prices the repeats from the published price book. It cannot tell how much of that input your provider had already cached at a discount, so the real figure is at most this and often less.",
            "**It only sees one process at a time.** The comparison happens in memory inside one SDK instance, so repeats spread across replicas, restarts or two languages are missed. Read the number as a floor.",
          ),
          note(
            "If you do not pass a customer or cache scope, Meter still counts the repeats but says reuse safety is unverified — nobody has told it the two calls belong to the same user or tenant, and it will not assume they do.",
          ),
          p(
            "Repeated requests and uncached prefixes often describe the same tokens, so Meter counts whichever is larger and drops the other rather than adding both.",
          ),
          note(
            "This finding used to be called **duplicate calls**, and was presented as a measured saving. The detection is the same idea and much stricter now — it compares the whole request rather than the prompt alone — but the old name asserted the repeats were avoidable, which is the one part Meter cannot check.",
          ),
        ],
      },
      {
        slug: "prompt-caching",
        title: "Prompt caching",
        summary:
          "What Meter counts before it tells you to cache a prompt — including what caching costs.",
        blocks: [
          p(
            "Most of what an AI feature sends is the same every call: a system prompt, tool definitions, a policy block. Providers will keep that static head in a cache and charge a fraction of the input rate to read it back. Meter looks for prefixes big enough and repeated often enough to be worth caching, and prices the change.",
          ),
          p("What goes into that number:"),
          list(
            "**How many calls shared the prefix**, and how many of them were already served from cache. Both are counts, not estimates.",
            "**How big the prefix is.** Where the provider reports the size of what it cached, that is the figure used. Where it does not, the SDK estimates from the request length and the finding says so — an estimated size is a ceiling, not a measured saving.",
            "**What caching would cost.** This is the part that decides whether there is a saving at all.",
          ),
          p(
            "Caching is not a discount, it is a trade. Reading a cached prefix is cheap; **writing** one costs more than sending it uncached — around a quarter more on a five-minute entry. Only the reads that follow pay that back. So Meter counts how many times the entry would have to be written: once for the first call, and again after every gap long enough for the provider to have dropped it.",
          ),
          note(
            "This is why a feature called a few times an hour gets no caching recommendation. The entry would expire before the next call, so every call would write and none would read, and turning caching on would raise the bill rather than lower it. A tool that priced only the discount would tell you to do it.",
          ),
          p(
            "It is also why a feature that **already** caches gets no recommendation. The calls that refresh a live cache report a cache write, and those are the cost of keeping it warm — not an opportunity to switch on something that is evidently already on.",
          ),
          table(
            ["You see", "It means"],
            [
              [
                "**Measured**, high confidence",
                "The provider reported the prefix size and your SDK counted the writes. Both sides of the trade are counted",
              ],
              [
                "**Up to**, med confidence",
                "Something is estimated rather than counted — usually the prefix size, or an SDK too old to count the writes. Upgrading it turns this into a measured figure",
              ],
              [
                "**Capped at your billed spend**",
                "The finding came out larger than the feature's bill. That should not happen, so the number is bounded by the invoice and the whole finding is treated as an upper bound",
              ],
            ],
          ),
          p(
            "Savings are priced from published list rates, not read off your invoice. If you have negotiated pricing, treat the figure as proportional rather than exact.",
          ),
          note(
            "Repeated requests and uncached prefixes often describe the same tokens, so Meter counts whichever is larger and drops the other rather than adding both.",
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
      {
        slug: "prompts",
        title: "Prompts",
        summary: "The prompts your product runs, what they cost, and how they get here.",
        blocks: [
          p(
            "Meter can only improve a prompt it has seen, and this is the one place it stores prompt text at all. Collection is **off** until someone in your organization turns it on deliberately — see [Prompt optimization and your prompts](/help/trust/prompt-optimization) for what that consent covers.",
          ),
          p(
            "With it on, the SDK sends the prompt template and a small number of short samples — never every call. Samples are encrypted, kept for 30 days, and the calls and cost shown on this screen come from metering rather than from the samples, so a handful of samples never stands in for your traffic.",
          ),
          p(
            "Opening a prompt's text is an act, and Meter records who did it and when. The screen shows **Hidden** until you ask for it.",
          ),
          note(
            "Withdrawing consent destroys the key those samples were encrypted with, which makes them unreadable rather than merely deleted.",
          ),
        ],
      },
      {
        slug: "testing-a-rewrite",
        title: "Testing a rewrite",
        summary: "A cheaper prompt is a suggestion until it has been replayed against real inputs.",
        blocks: [
          p(
            "Meter proposes a shorter prompt with a reason for every change. Until it has been tested it carries no dollar figure and says it is untested, because a saving nobody has measured is a guess.",
          ),
          p(
            "Testing replays real captured inputs through both the current prompt and the rewrite on your own model, then compares the answers: deterministic checks first, then a model asked to judge each pair **both ways round**, so a judge that contradicts itself scores a tie rather than a win.",
          ),
          p(
            "The decision rule belongs to Meter, not to the judge. A rewrite is recommended only when nothing came back broken and it is not worse on balance — and the saving you are shown is per call, scaled by your metered traffic.",
          ),
          note(
            "Replaying makes real model calls, so it needs a key that can make them; the read-only cost-API key cannot. A monthly cap bounds what testing can spend, and the screen shows the estimate and what is left before anything is charged.",
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
            "Scope a run-level alert to an **AI application** to alert per instrumented service. Provider and model scopes are not offered here, because a single run can call several providers and belongs wholly to none of them.",
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
            "**From the SDK**: token counts, model, feature id, latency. Never prompt or response content. The one exception is [Prompt optimization](/help/trust/prompt-optimization), and only with your consent.",
          ),
          p(
            "Credentials are encrypted before storage using your own deployment secret, and are never returned by an API or shown again in the UI.",
          ),
        ],
      },
      {
        slug: "prompt-optimization",
        title: "Prompt optimization and your prompts",
        summary:
          "The one feature that collects prompts: only with consent, per feature, for 30 days.",
        blocks: [
          p(
            "Everywhere else, Meter never holds prompt or response text. Prompt optimization is the one exception, because proposing a cheaper prompt and testing it needs the prompt. It collects nothing until someone in your organization agrees to its terms in [Settings](/settings), re-enters their password, and chooses which features may be captured.",
          ),
          list(
            "**What is collected**: the prompt instructions your developers wrote, and a small sample of real inputs and outputs for the chosen features. Never tool calls or their results, images or files.",
            "**Encrypted** with a key unique to your organization, and **deleted after 30 days**.",
            "**Who sees prompts** is named on the consent screen: your organization's own model if you set one under Bring your own key, otherwise Meter's. If that changes, capture pauses until someone agrees again.",
            "**Every view is logged.** The activity list in Settings shows who did what, never the prompts themselves.",
            "**Meter staff** viewing your account for support cannot turn capture on, withdraw it, or read captured prompts.",
            "**A developer has to switch it on too**: `capture_prompts` in the SDK (2.1 or later), with each prompt named by `prompt_id` and `prompt_version`. Consent alone sends nothing, and so does the switch alone.",
          ),
          note(
            "Withdrawing consent deletes everything collected immediately and destroys the encryption key, so no copy that survives anywhere, including a backup, can be read.",
          ),
        ],
      },
      {
        slug: "coding-agents",
        title: "Coding agents (MCP)",
        summary:
          "Connect Claude Code, or another MCP client, to read your numbers from your editor.",
        blocks: [
          p(
            "A coding agent — Claude Code, say — can ask Meter what a feature costs and what Meter has already found wrong with it, from inside the repository that produced the spend. That is the one place the answer is directly actionable: the agent can see the finding and the code at the same time.",
          ),
          p("Connect one under **Coding agents** in [Settings](/settings)."),
          steps(
            "Create a token and name it after the machine it is for. One per laptop, so you can turn one off without disturbing the others.",
            "Copy the token. It is shown once — Meter stores only a hash of it — so if you lose it, revoke it and make another.",
            "Paste the connection command the screen shows into Claude Code — or configure any other MCP client with the same URL and token.",
          ),
          p("What an agent can do with it is deliberately narrow:"),
          list(
            "**Read three things**: cost and token metrics over a period, the optimization opportunities Meter has already detected, and the evidence behind one of them.",
            "**Nothing else, and nothing at all can be changed.** There is no way to apply an optimization, rename a feature, edit a setting or trigger a sync. The connection Meter opens to answer these questions cannot write, whatever it is asked.",
            "**Only your organization.** The token identifies the organization, and the agent cannot name a different one.",
          ),
          p(
            "**Where the numbers go.** An agent reads your feature names, spend and customer identifiers, and sends them to whichever model that agent runs on. That model is outside Meter, and Meter's promises do not cover it — connect an agent only where that is acceptable. It never receives prompts or responses, because Meter does not store any.",
          ),
          note(
            'Every tool call is recorded and shown in the same place: what was asked for, by which token, and when. The trail keeps the question and never the answer. Revoking a token takes effect immediately and keeps its history, so "it was in use until Tuesday" stays answerable.',
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
            "[Settings](/settings) holds your organization profile and privacy preferences: display name, time zone, reporting currency, how customer identifiers are stored, how long traces are kept, your data retention window, and whether Prompt optimization may collect prompts.",
          ),
          note(
            "Traces never carry prompt or response content, and no setting changes that. Prompts are collected only through [Prompt optimization](/help/trust/prompt-optimization), with consent.",
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
        slug: "otel-not-reporting",
        title: "OpenTelemetry is configured but nothing appears",
        summary: "The wrong variable, an unencoded header, batching delay, or no token counts.",
        blocks: [
          steps(
            "**Read your exporter's log.** Unlike the SDK, an OpenTelemetry exporter reports failures. A `401` means the token header is wrong; a `404` means the URL is.",
            "**Use the traces-specific variable.** `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` takes the full URL as it is. The general `OTEL_EXPORTER_OTLP_ENDPOINT` appends `/v1/traces` itself, so given the full URL it asks for the path twice.",
            "**Encode the header.** Write `Authorization=Bearer%20<token>`. The value is parsed as W3C baggage, where a bare space is not allowed.",
            "**Wait a few seconds.** Exporters batch spans and send them on a timer.",
            "**Check the spans carry token counts.** A span with no `gen_ai.usage` attributes is recorded as a step but costs nothing, because there is nothing to price.",
          ),
          p(
            "If runs appear but all of their cost is Unattributed, the feature is not reaching the model-call spans. See [Using OpenTelemetry instead](/help/sdk/opentelemetry).",
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
            "**Classify your keys** on [Connect sources](/cost-sources) so spend at least separates production from development.",
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
