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

#: The rule table, in priority order. First match wins.
RULES: tuple[Rule, ...] = (
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


def classify(item: LineItem) -> Classification:
    """The one primary category for a line item, plus the rule that decided it.

    An item with no service name at all is `unclassified` — the bill told us
    nothing to classify by, and saying "infrastructure" would be inventing an
    answer. It is still stored, with its dimensions, so it can be reprocessed.
    """
    if not (item.service or "").strip():
        return Classification("unclassified", "no-service-dimension")
    for rule in RULES:
        if rule.matches(item):
            return Classification(rule.category, rule.name, rule.dedupe_owner)
    return Classification(DEFAULT_CATEGORY, DEFAULT_RULE)


def rule_table() -> list[dict]:
    """The rule table as plain data — for docs, tests, and the setup UI."""
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
        for r in RULES
    ]
