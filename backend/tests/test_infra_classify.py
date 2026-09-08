"""The classifier: one category per line item, and never a dropped row."""

from __future__ import annotations

import pytest
from meter.infra_classify import CATEGORIES, LineItem, classify, rule_table


def cat(**kwargs) -> str:
    return classify(LineItem(**kwargs)).category


# --- inference -------------------------------------------------------------
@pytest.mark.parametrize(
    "service",
    ["Amazon Bedrock", "Amazon Bedrock Agents", "Claude 3.5 Sonnet (Amazon Bedrock Edition)"],
)
def test_bedrock_is_inference_and_owned_by_the_bedrock_connector(service):
    verdict = classify(LineItem(service=service))
    assert verdict.category == "inference"
    # The marker the ingest uses to keep these dollars out of infra totals.
    assert verdict.dedupe_owner == "bedrock"
    assert verdict.rule == "bedrock-inference"


def test_a_managed_foundation_model_endpoint_is_inference():
    assert cat(service="Amazon SageMaker", operation="JumpStart:Invoke") == "inference"


# --- self-hosted -----------------------------------------------------------
@pytest.mark.parametrize(
    "usage_type",
    ["USE1-BoxUsage:p4d.24xlarge", "BoxUsage:g5.12xlarge", "EUW1-BoxUsage:inf2.8xlarge"],
)
def test_gpu_instance_families_are_self_hosted(usage_type):
    assert cat(service="Amazon Elastic Compute Cloud - Compute", usage_type=usage_type) == (
        "self_hosted"
    )


def test_a_cpu_instance_is_not_guessed_to_be_model_serving():
    # Nothing in the bill says what an m5 box runs. Guessing "self-hosted model"
    # would invent a number; infrastructure is the honest answer.
    assert (
        cat(service="Amazon Elastic Compute Cloud - Compute", usage_type="USE1-BoxUsage:m5.large")
        == "infrastructure"
    )


def test_sagemaker_gpu_needs_both_the_hardware_and_a_serving_operation():
    gpu = "USE1-Host:ml.g5.12xlarge"
    assert cat(service="Amazon SageMaker", usage_type=gpu, operation="Hosting") == "self_hosted"
    # Training on the same hardware is not model serving.
    assert cat(service="Amazon SageMaker", usage_type=gpu, operation="Training") == (
        "infrastructure"
    )


# --- build -----------------------------------------------------------------
@pytest.mark.parametrize(
    "service", ["AWS CodeBuild", "AWS CodePipeline", "Amazon CodeCatalyst", "Amazon Q Developer"]
)
def test_ci_and_developer_tooling_is_build(service):
    assert cat(service=service) == "build"


# --- the default -----------------------------------------------------------
@pytest.mark.parametrize(
    "service",
    [
        "Amazon Simple Storage Service",
        "Amazon Relational Database Service",
        "AWS Something Launched Last Tuesday",
    ],
)
def test_unknown_and_ordinary_services_default_to_infrastructure(service):
    # The point of the default: a service nobody has heard of still shows up as
    # money, instead of vanishing from the bill.
    assert cat(service=service) == "infrastructure"


def test_an_item_with_no_service_is_unclassified_not_invented():
    verdict = classify(LineItem(service=""))
    assert verdict.category == "unclassified"
    assert verdict.rule == "no-service-dimension"


# --- properties of the table ----------------------------------------------
def test_every_item_gets_exactly_one_known_category():
    for item in [
        LineItem(service="Amazon Bedrock"),
        LineItem(service=""),
        LineItem(service="Anything At All"),
        LineItem(service="AWS CodeBuild", usage_type="USE1-BoxUsage:p4d.24xlarge"),
    ]:
        assert classify(item).category in CATEGORIES


def test_rules_are_data_and_every_one_is_explainable():
    table = rule_table()
    assert table, "the rule table must not be empty"
    for rule in table:
        assert rule["category"] in CATEGORIES
        assert rule["why"], f"rule {rule['name']} has no stated reason"
    # Names are unique — the name is what gets stored on the row.
    names = [r["name"] for r in table]
    assert len(names) == len(set(names))


# --- Azure -----------------------------------------------------------------
def acat(**kwargs) -> str:
    return classify(LineItem(**kwargs), "azure_cloud").category


@pytest.mark.parametrize("service", ["Azure OpenAI", "Cognitive Services"])
def test_azure_openai_is_inference_and_owned_by_the_azure_connector(service):
    verdict = classify(LineItem(service=service), "azure_cloud")
    assert verdict.category == "inference"
    # The existing "Azure OpenAI (Azure cost)" connector owns these dollars.
    assert verdict.dedupe_owner == "azure"


@pytest.mark.parametrize(
    "size", ["Standard_NC24ads_A100_v4", "ND96asr_v4", "Standard_NV12s_v3", "NC6s_v3"]
)
def test_azure_n_series_vms_are_self_hosted(size):
    assert acat(service="Virtual Machines", usage_type=size) == "self_hosted"


def test_an_ordinary_azure_vm_is_not_guessed_to_be_model_serving():
    assert acat(service="Virtual Machines", usage_type="Standard_D4s_v5") == "infrastructure"


def test_azure_devops_is_build():
    assert acat(service="Azure DevOps") == "build"


def test_unknown_azure_services_default_to_infrastructure():
    assert acat(service="Azure Cosmos DB") == "infrastructure"
    assert acat(service="Some Azure Thing From Next Year") == "infrastructure"


# --- GCP -------------------------------------------------------------------
def gcat(**kwargs) -> str:
    return classify(LineItem(**kwargs), "gcp").category


@pytest.mark.parametrize("service", ["Vertex AI", "Generative Language API"])
def test_vertex_is_inference_and_owned_by_the_google_connector(service):
    verdict = classify(LineItem(service=service), "gcp")
    assert verdict.category == "inference"
    assert verdict.dedupe_owner == "google"


def test_gcp_accelerator_skus_are_self_hosted():
    assert (
        gcat(service="Compute Engine", usage_type="Nvidia Tesla A100 GPU running in Americas")
        == "self_hosted"
    )
    assert gcat(service="Kubernetes Engine", usage_type="Cloud TPU v5e chip-hour") == "self_hosted"


def test_a_plain_gcp_instance_is_not_guessed_to_be_model_serving():
    assert (
        gcat(service="Compute Engine", usage_type="N1 Predefined Instance Core running in Americas")
        == "infrastructure"
    )


def test_cloud_build_is_build():
    assert gcat(service="Cloud Build") == "build"


def test_unknown_gcp_services_default_to_infrastructure():
    assert gcat(service="Cloud Spanner") == "infrastructure"
    assert gcat(service="Whatever Google Ships Next") == "infrastructure"


# --- the tables do not bleed into one another ------------------------------
def test_a_provider_is_never_classified_by_another_s_vocabulary():
    # "Compute Engine" is a GCP service. Read as AWS it must not match a GCP
    # rule — it is simply an unfamiliar service, which is infrastructure.
    gpu = LineItem(service="Compute Engine", usage_type="Nvidia Tesla A100 GPU")
    assert classify(gpu, "gcp").category == "self_hosted"
    assert classify(gpu, "aws").category == "infrastructure"
    # And AWS's own vocabulary means nothing on an Azure bill.
    assert classify(LineItem(service="Amazon Bedrock"), "azure_cloud").category == "infrastructure"


def test_a_provider_with_no_rules_still_classifies_rather_than_dropping():
    verdict = classify(LineItem(service="Some Service"), "oracle_cloud")
    assert verdict.category == "infrastructure"
    assert verdict.rule == "default-infrastructure"


@pytest.mark.parametrize("provider", ["aws", "azure_cloud", "gcp"])
def test_every_provider_table_is_data_and_explainable(provider):
    table = rule_table(provider)
    assert table, f"{provider} has no rules"
    names = [r["name"] for r in table]
    assert len(names) == len(set(names))
    for rule in table:
        assert rule["category"] in CATEGORIES
        assert rule["why"], f"{provider} rule {rule['name']} has no stated reason"


@pytest.mark.parametrize("provider", ["aws", "azure_cloud", "gcp"])
def test_every_provider_has_exactly_one_deduped_inference_owner(provider):
    # Each cloud bills exactly one model service that another connector already
    # ingests. More than one owner here would mean an ambiguous dedupe.
    owners = {r["dedupe_owner"] for r in rule_table(provider) if r["dedupe_owner"]}
    assert len(owners) == 1, f"{provider} has dedupe owners {owners}"


@pytest.mark.parametrize("provider", ["aws", "azure_cloud", "gcp"])
def test_no_provider_ever_drops_an_item(provider):
    for item in [LineItem(service="x"), LineItem(service=""), LineItem(service="Totally Unknown")]:
        assert classify(item, provider).category in CATEGORIES
