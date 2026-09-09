"""Which category a cloud billing line item belongs to.

A cloud bill is one undifferentiated pile of dollars. Meter's model is that
every dollar is *either* build cost or run cost, and run cost splits further
into inference, self-hosted model serving, and the ordinary infrastructure
around them. Something has to make that call for each line item, and this
module is the only place that does.

Three rules govern it, and they matter more than the rule table itself:

  1. **One primary category per item.** Never two, never a share of each. An
     item with two plausible categories takes the first matching rule, and the
     rule's name is stored beside the row so the answer is explainable.

  2. **An unknown service is infrastructure, not a dropped row.** AWS ships new
     services constantly. A classifier that only recognized a fixed list would
     silently lose money from the bill every time Amazon launched something,
     which is exactly the black-box behaviour this product exists to replace.
     The default is `infrastructure` and it is a real answer, not a fallback.

  3. **Guessing is worse than not knowing.** `self_hosted` and `build` are only
     assigned where the bill itself says so — a GPU instance family in the usage
     type, a service that is unambiguously a CI/CD or developer tool. Where the
     evidence is a hunch ("this EC2 box is *probably* serving a model"), the
     item stays `infrastructure` and a person can reclassify it. The raw service
     and dimensions are preserved on every row so that stays possible.

Rules are data. Adding one is appending a `Rule` to `RULES`, not editing
control flow, and the tests below this module read the same table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

#: Every category a line item can be given. Mirrors the CHECK on infra_cost.category.
CATEGORIES = ("inference", "self_hosted", "infrastructure", "build", "unclassified")

#: What an item gets when no rule matches. See rule 2 in the module docstring.
DEFAULT_CATEGORY = "infrastructure"
DEFAULT_RULE = "default-infrastructure"


@dataclass(frozen=True)
class LineItem:
    """The dimensions of one cloud billing line item, as billed.

    Only `service` is required; every cost API returns some dimensions and not
    others depending on how the query was grouped, and a rule that needs a
    dimension the query did not ask for simply does not match.
    """

    service: str = ""
    usage_type: Optional[str] = None
    operation: Optional[str] = None
    region: Optional[str] = None
    account_id: Optional[str] = None
    tag_value: Optional[str] = None


@dataclass(frozen=True)
class Classification:
    category: str
    #: Which rule decided this, stored on the row so the number is explainable.
    rule: str
    #: Set when another connector is the authoritative source for these dollars.
    #: The ingest writes such rows with counted=false so nothing is counted twice.
    dedupe_owner: Optional[str] = None


@dataclass(frozen=True)
class Rule:
    """One classification rule. All stated conditions must match.

    `services` matches the provider's service name case-insensitively, by
    substring, so "Amazon Bedrock" also catches AWS's occasional suffixed
    variants ("Amazon Bedrock Agents", "Claude 3.5 Sonnet (Amazon Bedrock
    Edition)"). `usage_type_pattern` / `operation_pattern` are regexes applied
    to those dimensions when the rule needs harder evidence than a service name.
    """

    name: str
    category: str
    services: tuple[str, ...] = ()
    usage_type_pattern: Optional[str] = None
    operation_pattern: Optional[str] = None
    dedupe_owner: Optional[str] = None
    #: Documented reason, so a reader of the table knows why the rule is safe.
    why: str = ""
    _compiled: dict = field(default_factory=dict, compare=False, repr=False)

    def matches(self, item: LineItem) -> bool:
        service = (item.service or "").casefold()
        if self.services and not any(s.casefold() in service for s in self.services):
            return False
        if self.usage_type_pattern and not _search(self.usage_type_pattern, item.usage_type):
            return False
        if self.operation_pattern and not _search(self.operation_pattern, item.operation):
            return False
        return True


def _search(pattern: str, value: Optional[str]) -> bool:
    return bool(value) and re.search(pattern, value, re.IGNORECASE) is not None


# GPU and accelerator instance families, as they appear inside an AWS usage type
# (e.g. "BoxUsage:p4d.24xlarge", "USE1-Host:ml.g5.12xlarge"). These are the only
# EC2/SageMaker shapes we will call model serving without being told: nothing
# else in the bill distinguishes a GPU box from a web server.
_GPU_FAMILIES = r"(?:^|[-:.])(?:p[2-9][a-z]*|g[3-9][a-z]*|inf[0-9]+|trn[0-9]+|dl[0-9]+)\."

#: AWS rules, in priority order. First match wins.
AWS_RULES: tuple[Rule, ...] = (
    # --- inference -------------------------------------------------------
    Rule(
        name="bedrock-inference",
        category="inference",
        services=("Amazon Bedrock", "AWS Bedrock"),
        dedupe_owner="bedrock",
        why=(
            "Bedrock is model inference billed through AWS. The existing Bedrock "
            "connector is its authoritative ingestion path, so these rows are "
            "recorded but not counted here."
        ),
    ),
    Rule(
        name="sagemaker-jumpstart-inference",
        category="inference",
        services=("Amazon SageMaker",),
        operation_pattern=r"jumpstart|bedrock",
        why="A managed foundation-model endpoint, priced per call, not per GPU-hour.",
    ),
    # --- self-hosted model serving --------------------------------------
    # Only where the usage type names an accelerator family AND the operation
    # says the box is serving, not training or idling in a notebook.
    Rule(
        name="sagemaker-gpu-endpoint",
        category="self_hosted",
        services=("Amazon SageMaker",),
        usage_type_pattern=_GPU_FAMILIES,
        operation_pattern=r"host|endpoint|inference",
        why="A SageMaker endpoint on accelerator hardware: a model you run yourself.",
    ),
    Rule(
        name="ec2-gpu-instance",
        category="self_hosted",
        services=("Elastic Compute Cloud", "Amazon EC2"),
        usage_type_pattern=_GPU_FAMILIES,
        why=(
            "The usage type names a GPU/accelerator instance family. A CPU EC2 box "
            "is deliberately NOT matched — nothing in the bill says what it runs."
        ),
    ),
    # --- build (CI/CD and developer tooling) -----------------------------
    # Named services only. These exist to build and ship software; their spend
    # is build cost by definition, not by inference about workload.
    Rule(
        name="aws-developer-tooling",
        category="build",
        services=(
            "CodeBuild",
            "CodePipeline",
            "CodeDeploy",
            "CodeCommit",
            "CodeArtifact",
            "CodeCatalyst",
            "CodeGuru",
            "CodeWhisperer",
            "Cloud9",
            "Amazon Q Developer",
            "AWS Amplify",
        ),
        why="CI/CD and developer tooling: spend that exists to build the product.",
    ),
)


# Azure names its accelerator sizes in the meter/SKU rather than the service
# ("Standard_NC24ads_A100_v4", "ND96asr v4"). The N-series is Azure's GPU family;
# nothing else in the bill distinguishes a GPU VM from a web server.
_AZURE_GPU_SIZES = r"\b(?:standard_)?N[CDVP][0-9]+|\bND[0-9]+|\bNC[0-9]+|\bNV[0-9]+"

#: Azure rules. Same three principles as AWS: one category, unknown ->
#: infrastructure, and no guessing where the bill does not say.
AZURE_RULES: tuple[Rule, ...] = (
    Rule(
        name="azure-openai-inference",
        category="inference",
        services=("Azure OpenAI", "Cognitive Services"),
        dedupe_owner="azure",
        why=(
            "Model inference billed through Azure. The existing 'Azure OpenAI "
            "(Azure cost)' connector is its authoritative ingestion path, so these "
            "rows are recorded but not counted here."
        ),
    ),
    Rule(
        name="azure-ml-gpu-endpoint",
        category="self_hosted",
        services=("Machine Learning", "Azure ML"),
        usage_type_pattern=_AZURE_GPU_SIZES,
        operation_pattern=r"inference|endpoint|online|deploy",
        why="An Azure ML online endpoint on GPU hardware: a model you run yourself.",
    ),
    Rule(
        name="azure-gpu-vm",
        category="self_hosted",
        services=("Virtual Machines", "Azure Kubernetes Service"),
        usage_type_pattern=_AZURE_GPU_SIZES,
        why=(
            "The meter names an N-series (GPU) VM size. A general-purpose VM is "
            "deliberately NOT matched — the bill does not say what it runs."
        ),
    ),
    Rule(
        name="azure-developer-tooling",
        category="build",
        services=("Azure DevOps", "DevTest Labs", "GitHub Advanced Security"),
        why="CI/CD and developer tooling: spend that exists to build the product.",
    ),
)

# GCP names accelerators in the SKU description ("Nvidia Tesla A100 GPU running
# in Americas", "Cloud TPU v5e chip-hour"), not in the service.
_GCP_ACCELERATORS = r"\b(?:GPU|TPU|Nvidia|A100|H100|L4|T4|V100)\b"

#: GCP rules. Line items come from the BigQuery billing export, where `service`
#: is the service description and `usage_type` is the SKU description.
GCP_RULES: tuple[Rule, ...] = (
    Rule(
        name="vertex-ai-inference",
        category="inference",
        services=("Vertex AI", "Generative Language", "AI Platform"),
        dedupe_owner="google",
        why=(
            "Model inference billed through GCP. The Google connector on the "
            "Inference tab owns these dollars, so they are recorded but not "
            "counted here — the two paths can never double-count."
        ),
    ),
    Rule(
        name="gcp-accelerator-compute",
        category="self_hosted",
        services=("Compute Engine", "Kubernetes Engine"),
        usage_type_pattern=_GCP_ACCELERATORS,
        why=(
            "The SKU names a GPU or TPU. A CPU instance is deliberately NOT "
            "matched — nothing in the bill says what it runs."
        ),
    ),
    Rule(
        name="gcp-developer-tooling",
        category="build",
        services=(
            "Cloud Build",
            "Artifact Registry",
            "Container Registry",
            "Cloud Source Repositories",
            "Cloud Deploy",
        ),
        why="CI/CD and developer tooling: spend that exists to build the product.",
    ),
)

# --- managed platforms -----------------------------------------------------
# These are narrower than a hyperscaler: a database platform bills databases and
# a CDN bills a CDN, so almost everything is infrastructure by definition. What
# the rules are for is the exception — the AI product each has bolted on, which
# is inference and must not be counted as infrastructure.

#: DigitalOcean. GPU Droplets are the one accelerator SKU; everything else —
#: Droplets, Spaces, Managed Databases, Load Balancers — is infrastructure.
DIGITALOCEAN_RULES: tuple[Rule, ...] = (
    Rule(
        name="do-gpu-droplet",
        category="self_hosted",
        services=("GPU Droplets", "Droplets"),
        usage_type_pattern=r"\bgpu\b|\bh100\b|\ba100\b|\bl40s\b|\bmi300x\b",
        why="The SKU names GPU hardware. An ordinary Droplet is NOT matched.",
    ),
    Rule(
        name="do-gradient-inference",
        category="inference",
        services=("GenAI Platform", "Gradient", "GradientAI"),
        why="DigitalOcean's managed model-serving product: inference, not infrastructure.",
    ),
    Rule(
        name="do-container-registry",
        category="build",
        services=("Container Registry",),
        why="An artifact store for builds — spend that exists to ship the product.",
    ),
)

#: MongoDB Atlas. A database platform: everything it bills is infrastructure.
#: The rule table is empty on purpose rather than absent, so the reason is
#: written down instead of looking like an oversight.
ATLAS_RULES: tuple[Rule, ...] = ()

#: Cloudflare. Workers AI is model inference billed by Cloudflare, and no other
#: connector owns it, so it is recorded as inference and simply stays out of
#: infrastructure totals.
CLOUDFLARE_RULES: tuple[Rule, ...] = (
    Rule(
        name="cloudflare-workers-ai",
        category="inference",
        services=("Workers AI", "AI Gateway", "Vectorize"),
        why=(
            "Cloudflare's model-serving products. No other connector ingests them, "
            "so they are counted as inference here rather than deduplicated away."
        ),
    ),
    Rule(
        name="cloudflare-pages-build",
        category="build",
        services=("Pages", "Cloudflare Pages"),
        why="Build minutes for deploying the product.",
    ),
)

#: Snowflake. SERVICE_TYPE is the dimension: AI_SERVICES is Cortex, Snowflake's
#: LLM layer. Warehouse compute and storage are infrastructure.
SNOWFLAKE_RULES: tuple[Rule, ...] = (
    Rule(
        name="snowflake-cortex-inference",
        category="inference",
        services=("AI_SERVICES", "CORTEX"),
        why=(
            "Snowflake Cortex is LLM inference billed by Snowflake. No other "
            "connector ingests it, so it is counted as inference here."
        ),
    ),
)

#: Vercel. FOCUS gives us ServiceName, so the rules read that. Two exceptions to
#: the infrastructure default, and they pull in opposite directions: build
#: minutes are build cost, and the AI Gateway is inference that the existing
#: "Vercel AI Gateway" connector already ingests.
VERCEL_RULES: tuple[Rule, ...] = (
    Rule(
        name="vercel-ai-gateway-inference",
        category="inference",
        services=("AI Gateway", "AI SDK", "Vercel AI"),
        dedupe_owner="vercel",
        why=(
            "Model spend routed through Vercel's AI Gateway. The existing Vercel "
            "AI Gateway connector on the Inference tab is its authoritative "
            "ingestion path, so these rows are recorded but not counted here."
        ),
    ),
    Rule(
        name="vercel-build-minutes",
        category="build",
        services=("Build", "Build Execution", "Build Minutes", "Remote Cache"),
        why=(
            "Vercel bills build execution by the minute. That is literally what a "
            "feature cost to build, on the run-cost side of the same invoice."
        ),
    ),
)

#: Which rule table applies to which infrastructure provider. A provider with no
#: entry gets no rules — every item defaults to `infrastructure`, which is still
#: a correct answer rather than a dropped row.
RULES_BY_PROVIDER: dict[str, tuple[Rule, ...]] = {
    "aws": AWS_RULES,
    "azure_cloud": AZURE_RULES,
    "gcp": GCP_RULES,
    "digitalocean": DIGITALOCEAN_RULES,
    "mongodb_atlas": ATLAS_RULES,
    "cloudflare": CLOUDFLARE_RULES,
    "snowflake": SNOWFLAKE_RULES,
    "vercel_cloud": VERCEL_RULES,
}

#: Back-compat alias: the AWS table was `RULES` when AWS was the only provider.
RULES = AWS_RULES


def classify(item: LineItem, provider: str = "aws") -> Classification:
    """The one primary category for a line item, plus the rule that decided it.

    Rules are per provider: "Compute Engine" means nothing on an AWS bill and
    "Amazon Bedrock" means nothing on a GCP one, so the tables are separate and
    a provider is never classified by another's vocabulary. A provider with no
    table yet still classifies — everything defaults to `infrastructure`, which
    is a correct answer rather than a dropped row.

    An item with no service name at all is `unclassified`: the bill told us
    nothing to classify by, and saying "infrastructure" would be inventing an
    answer. It is still stored, with its dimensions, so it can be reprocessed.
    """
    if not (item.service or "").strip():
        return Classification("unclassified", "no-service-dimension")
    for rule in RULES_BY_PROVIDER.get(provider, ()):
        if rule.matches(item):
            return Classification(rule.category, rule.name, rule.dedupe_owner)
    return Classification(DEFAULT_CATEGORY, DEFAULT_RULE)


def rule_table(provider: str = "aws") -> list[dict]:
    """One provider's rules as plain data — for docs, tests, and the setup UI."""
    return [
        {
            "name": r.name,
            "category": r.category,
            "services": list(r.services),
            "usage_type_pattern": r.usage_type_pattern,
            "operation_pattern": r.operation_pattern,
            "dedupe_owner": r.dedupe_owner,
            "why": r.why,
        }
        for r in RULES_BY_PROVIDER.get(provider, ())
    ]
