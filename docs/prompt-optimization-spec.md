# Spec — Prompt Optimization (consented prompt capture)

> **Status:** draft for review, 2026-09-11. Extends
> [`optimization-opportunities-spec.md`](optimization-opportunities-spec.md) §12
> ("Eval-backed model downgrade … requires prompt capture → explicit opt-in and
> careful handling"). Decisions below were made by the founder on 2026-09-11.

## 0. In plain language

Meter will be able to take a prompt a customer's product uses, propose a cheaper
version of it, explain every change, test the new version against real examples,
and show what it would save. It does this **only** for features where a customer
admin has explicitly agreed to let Meter collect prompts.

This is the first time Meter holds prompt text. Until now the product's promise
was that it never does, and that promise is written into code: the SDK has no
field for content, the server rejects events that carry it, and the
OpenTelemetry and Splunk routes drop or strip it. This feature does **not**
loosen any of that. Consented prompts travel on a separate, new channel that only
opens when the customer turns it on, feature by feature, and that deletes
everything it holds after 30 days, or immediately if consent is withdrawn.

## 1. Decisions (founder, 2026-09-11)

| Question | Decision |
|---|---|
| What may be collected, with consent | **Prompt templates plus short-lived samples** of real inputs and outputs, kept only for testing. Encrypted, auto-deleted after 30 days, consent per feature, every access logged. |
| Where prompts come from (v1) | **SDK opt-in first.** OpenTelemetry and Splunk keep stripping content; they can follow later. |
| Which model writes and judges | **The customer's own key if they have configured one** (the BYOK setting discovery already uses); **otherwise Meter's model**. The consent screen names the provider and model that will see prompts. |
| How an optimization is proven | **Replay and compare, gated.** Sampled real inputs are run through the old and the new prompt; cost and latency are measured, quality is judged, and nothing is recommended unless quality holds. Each run that spends tokens needs approval first. |

## 2. Principles

1. **Double key.** Content flows only when the customer has consented in Meter
   **and** the developer has turned capture on in the SDK. Either one alone sends
   and stores nothing.
2. **The trace path stays content-free.** `traces.FORBIDDEN_FIELDS`, the OTLP
   translator's allowlist and the Splunk content-stripping pipeline are unchanged.
   Prompt samples never enter `ai_trace`, `ai_span`, cost tables, logs, alerts,
   the assistant's data snapshot, the admin portal or exports.
3. **Minimum, and short-lived.** Sampled, size-capped, 30-day retention, and a
   revocation that destroys the key so nothing captured remains readable,
   including in backups.
4. **No black-box rewrites.** Every proposed change carries a stated reason and an
   expected effect; every number (tokens, dollars, latency) is measured or priced
   by `pricing.py`, never asserted by a model.
5. **Not worse, or not recommended.** A candidate is shown as *recommended* only
   after an evaluation passes. Unevaluated candidates are labelled as such.
6. **The customer pays for evaluation knowingly.** Replays cost tokens. Each run
   shows an estimate, needs approval, and respects a monthly cap.

## 3. Consent

### 3.1 Organization consent

A user of the organization turns Prompt Optimization on in **Settings → Privacy &
data**. Meter has no roles inside a tenant today, so the safeguard is
deliberateness: the user **re-enters their password** and ticks an explicit
statement. The consent screen states, in plain words:

- what is collected (templates, sampled inputs and outputs) and what is not (tool
  calls and results, images, anything outside consenting features);
- that samples are encrypted and deleted after 30 days;
- **which provider and model will see prompts** — the org's own configured
  provider and model if set, otherwise Meter's (today: Groq, `openai/gpt-oss-120b`);
- that evaluations spend tokens on the customer's provider account, only after
  approval, within a monthly cap;
- that anyone in the organization can view captured samples, and every view is
  logged;
- how to withdraw, and that withdrawing deletes everything immediately.

Recorded: who, when, the consent text version (`PROMPT_CAPTURE_CONSENT_VERSION`),
and the disclosed model/provider. If the disclosed model changes (BYOK added,
removed or pointed elsewhere), capture pauses until someone re-consents to the
new disclosure.

### 3.2 Feature consent

After organization consent, capture is enabled **per feature**. A feature not
switched on captures nothing, whatever the SDK is configured to do.

### 3.3 Withdrawal

Turning a feature off stops capture for it and deletes its samples, candidates
and evaluations. Turning the organization off does that for every feature and
**deletes the tenant's data key** (§5), which makes any remaining ciphertext —
including in database backups — unreadable. The audit log (§8) is kept.

### 3.4 What happens to the old settings

- `tenant.store_prompts` (0027) is a no-op toggle today and must **not** become
  the consent mechanism. PO-1 removes it from the UI; the column stays.
- `tenant.content_capture` (0049) keeps meaning *the trace path*, and stays
  enforced at `disabled`. The Settings copy changes from "cannot be enabled" to
  explaining that traces never carry content, and that prompt samples for
  optimization are collected only for consenting features.

## 4. Capture (SDK)

A new, explicit SDK option, off by default:

```python
meter = Meter(capture_prompts=True)          # the developer's half of the double key
client = meter.wrap(anthropic, feature_id="...", prompt_id="triage", prompt_version="v7")
```

- **Sampled.** Default 1 call in 100, at most 50 samples a day per prompt, at most
  200 kept per prompt version. The server enforces the caps whatever the client says.
- **Named.** A sample must carry `feature_id`, `prompt_id` and `prompt_version`;
  optimization targets a named prompt, and the Prove loop (§9) needs versions.
- **What a sample holds.** The template (system / instruction text, stored once per
  prompt version), the variable input messages, the model's text output, provider,
  model, request parameters that change behaviour (temperature, max tokens,
  response format), and usage (tokens, latency).
- **What it never holds.** Tool-use and tool-result blocks, images and files, and
  retrieved documents passed as separate parts. Context the application pastes
  into a message's *text* cannot be told apart and is included — the consent
  screen says so.
- **Size.** A sample over 64 KB is dropped, not truncated: a truncated prompt would
  be evaluated as a different prompt.
- **Client-side redaction hook.** `redact=callable` runs on each sample before it is
  queued, for customers who want to scrub known identifiers first.
- **Separate channel.** Its own endpoint (`POST /api/prompt-capture/samples`), its
  own queue, its own failure accounting. A capture failure never affects metering,
  and metering never waits on capture.
- The SDK asks the server whether capture is open for a feature (cached briefly);
  the server re-checks consent on every sample and refuses otherwise.
- **v1 scope (SDK 2.1, PO-2).** Capture works through `meter.wrap(...)` for
  Anthropic `messages.create` and OpenAI `chat.completions.create`, the two shapes
  whose text blocks can be told apart from tool calls reliably. The template is the
  system prompt (Anthropic `system`; OpenAI system and developer messages); a call
  with no instruction text is not sampled. `run.llm(...)` steps cannot be sampled,
  because the request is hidden inside the function passed in. The first call for
  a feature only asks whether capture is open, and is itself never sampled.

## 5. Storage and security

- **Envelope encryption.** Each consenting tenant gets a random data key, stored
  only encrypted by the app key (`crypto.encrypt`). Sample text, templates,
  candidate prompts and replay outputs are encrypted with the tenant data key.
  Deleting the data key is the crypto-shred in §3.3.
- **Tables (RLS, `FORCE ROW LEVEL SECURITY`, like `recon_*`):**
  `prompt_capture_consent` (org consent: actor, version, disclosed model,
  granted/withdrawn at), `prompt_capture_feature` (per-feature switch),
  `prompt_data_key`, `prompt_template` (per prompt_id + version, encrypted),
  `prompt_sample` (encrypted input/output + plaintext usage numbers),
  `prompt_candidate`, `prompt_evaluation`, `prompt_evaluation_case`,
  `prompt_audit` (append-only).
- **Reading content is explicit.** List endpoints return identities and numbers
  only; content is returned by a separate "show" request, which is audited.
- **Never in logs.** Provider errors are redacted before logging (as
  `discovery_llm.redact` does for keys), and request bodies are never logged.
- **Retention.** A daily purge (the same job as trace retention) deletes samples,
  replay outputs and candidates older than 30 days.
- **Session replay.** Any element showing content carries the same masking class
  as the ingest token snippet.

## 6. The optimizer

For a prompt version with enough samples (default ≥ 20), Meter asks a model to
propose a cheaper template.

- **Model.** `discovery_llm.active_config(tenant)` if the tenant has BYOK,
  otherwise `env_llm_config()` — exactly the model named at consent time.
- **Techniques it may use,** each a named category so the reason is legible:
  remove redundant or repeated instructions; compress verbose examples; move
  static text ahead of variable text so it can be cached; tighten the requested
  output format to cut output tokens; remove instructions the samples show are
  never exercised.
- **Structured output.** The model returns the new template and a list of changes,
  each `{category, before_excerpt, after_excerpt, reason, expected_effect}`.
  Output that does not parse, or a template missing a variable the original used,
  is discarded.
- **Numbers are Meter's.** Input-token savings per call are computed by counting
  both templates with the provider's tokenizer where available, otherwise by
  replay (§7). The model's "expected effect" is labelled as its expectation.
- **Status:** a candidate starts as *not evaluated*.

## 7. Evaluation — replay and compare, gated

**Replay runs on the model the feature actually uses.** A feature on Anthropic
Sonnet must be tested on Anthropic Sonnet; Meter's own model cannot stand in for
it. Replay therefore needs an **inference-capable key for that provider**:

- the BYOK configuration, when it points at the same provider and model family; or
- an evaluation key the customer adds for that provider (encrypted like a
  connector credential, used only for replays).

Without one, a candidate can be generated and explained but not evaluated, and is
never shown as recommended. *(Open question 1.)*

**A run:**

1. Meter picks K sampled inputs (default 30, stratified across the period) and
   shows an estimate: `K × (original + candidate) × average tokens`, priced, plus
   the judge's cost. A user approves. A monthly evaluation cap (default $25)
   blocks runs that would exceed it.
2. Each input is replayed with the original and the candidate template at the
   sample's original parameters. Tokens, latency and priced cost are recorded per case.
3. **Deterministic checks** per case: output parses when the original's did
   (JSON), no refusal where the original answered, required structure present,
   length within bounds.
4. **Pairwise judge** per case (the optimizer model): both orders, to cancel
   position bias; a verdict of better / same / worse with a one-line reason.
5. **Decision rule.** *Recommended* only if: no deterministic regressions, worse
   verdicts ≤ better verdicts + 10% of K, and K ≥ 20 completed. Otherwise *not
   recommended*, with the failing cases shown.

**Results shown:** per-call input/output tokens and cost, old vs new; latency;
win / same / worse counts; each case side by side (content behind "show", audited);
the reasons for failures.

## 8. Audit

Append-only `prompt_audit`, like `recon_audit`: consent granted / withdrawn,
feature on / off, disclosure changed, sample content shown, candidate generated,
evaluation estimated / approved / completed, retention purge, data key destroyed.
Every row carries the actor and time; none carries content.

## 9. Benefit and the Prove loop

- **Projected:** measured per-call saving from the evaluation × the prompt
  version's real monthly call volume (from `ai_span`, by `prompt_id` and
  `prompt_version`), priced by `pricing.py`. It enters the Copilot as a new lever,
  `prompt_optimization`, with `savings_type = modeled_ceiling`: measured on a
  sample, realised only if traffic looks like the sample.
- **Applied:** the customer ships the new template under a **new
  `prompt_version`**. Meter sees the version change in traces.
- **Verified:** cost per call for the new version vs the old, over matched periods —
  the existing Prove loop (opt spec §20), keyed on prompt version instead of a
  lever's signal.

## 10. Screens

- **Settings → Privacy & data:** the consent flow (§3.1), the disclosed model, a
  per-feature switch list, "withdraw and delete everything", and the audit log.
- **Optimize → Prompts** (new tab on the Copilot): each captured prompt version —
  feature, calls per month, samples held, candidate status, projected saving.
- **Prompt detail:** existing and optimized template side by side with a diff;
  the list of changes with reasons; the evaluation (estimate, approve, results,
  per-case comparison); "mark applied" and "discard".
- **Install SDK:** a section on `capture_prompts`, shown only after organization
  consent exists.

## 11. Milestones

Each ends with a review.

- **PO-1 — Consent, encrypted store, retention, withdrawal.** Migrations for the
  tables in §5; envelope encryption with crypto-shred; organization and feature
  consent APIs (password re-entry, versioned text, disclosed model); the capture
  endpoint that stores samples only when both consents hold and caps are
  respected; the 30-day purge in the daily job; audit; the Settings consent UI;
  removal of the no-op `store_prompts` toggle. No SDK change, no model calls.
  *Accept:* a sample is refused without org consent, without feature consent, over
  the size cap and over the daily cap; a stored sample's text is not readable in
  the database without the tenant data key; withdrawal deletes the key and all
  content; purge removes content older than 30 days; every action writes an audit
  row with no content in it; tenant isolation holds; content never appears in
  logs, the assistant snapshot, the admin portal or trace APIs.
- **PO-2 — SDK capture channel (Python + Node).** `capture_prompts`, sampling,
  caps, `redact` hook, separate queue and endpoint, consent check. SDK minor
  version bump (publishing is asked about separately).
  *Accept:* off by default; nothing sent without server-side consent; a capture
  failure never affects metering; tool blocks and images never sent; oversized
  samples dropped.
- **PO-3 — Optimizer and prompt screens.** Candidate generation with structured,
  validated changes; token counts; Optimize → Prompts and prompt detail with diff
  and reasons; candidates labelled *not evaluated*.
- **PO-4 — Evaluation.** Evaluation keys; estimate, approval, monthly cap; replay;
  deterministic checks; pairwise judge; decision rule; results UI.
- **PO-5 — Copilot and Prove loop.** The `prompt_optimization` lever, projected
  savings, prompt-version-based verification, handbook topics, demo data.

## 12. Decisions and open questions

**Decided (founder, 2026-09-11, after PO-1 review):**

1. **Replay without a provider key.** A candidate can be generated and explained
   without one, but is **never shown as recommended** until an inference-capable
   key for the feature's provider exists and an evaluation has passed.
2. **Who may consent.** Anyone in the organization, with **password re-entry** as
   the safeguard. An organization admin role remains a possible later change,
   not a prerequisite.

**Still open:**

3. **Other sources.** OpenTelemetry (and Splunk, whose guide strips content) could
   feed samples later, behind the same consent, via a separate content pipeline.
4. **Tokenizers.** Exact template token counts need per-provider tokenizers;
   until then input-token deltas come from replay usage.
