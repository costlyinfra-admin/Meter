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
