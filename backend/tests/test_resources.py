"""Shared resource-classification model: default, persistence, sync-safety."""

from __future__ import annotations

import pytest
from meter import resources


def _register(tenant_id, name="service-a-prod", key="k_a"):
    resources.register_resources(
        tenant_id,
        "anthropic",
        [
            {"resource_type": "workspace", "resource_id": "ws_mcs", "resource_name": "mcs-dev"},
            {
                "resource_type": "api_key",
                "resource_id": key,
                "resource_name": name,
                "parent_resource_id": "ws_mcs",
            },
        ],
    )


def test_new_resource_defaults_unclassified_no_name_inference(tenant_id):
    # A key literally named "…-prod" must NOT auto-classify as production.
    _register(tenant_id)
    classes = resources.get_classifications(tenant_id, "anthropic")
    assert classes[("api_key", "k_a")] == "unclassified"
    assert classes[("workspace", "ws_mcs")] == "unclassified"


@pytest.mark.parametrize(
    "value", ["production", "development", "internal", "ignore", "unclassified"]
)
def test_user_can_choose_any_of_the_five(tenant_id, value):
    _register(tenant_id)
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", value)
    assert resources.get_classifications(tenant_id, "anthropic")[("api_key", "k_a")] == value


def test_invalid_classification_rejected(tenant_id):
    _register(tenant_id)
    with pytest.raises(resources.ResourceError):
        resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", "prod")


def test_sync_never_overwrites_manual_classification(tenant_id):
    _register(tenant_id)
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", "production")
    # A later sync re-registers the same resource (maybe with a new name)...
    _register(tenant_id, name="service-a-prod (renamed)")
    classes = resources.get_classifications(tenant_id, "anthropic")
    assert classes[("api_key", "k_a")] == "production"  # choice preserved
    # ...but the display name refreshed.
    listed = {r["resource_id"]: r for r in resources.list_resources(tenant_id, "anthropic")}
    assert listed["k_a"]["resource_name"] == "service-a-prod (renamed)"


def test_disappearing_and_reappearing_resource_keeps_mapping(tenant_id):
    _register(tenant_id)
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", "internal")
    # A sync that doesn't include k_a (temporarily gone) doesn't erase it...
    resources.register_resources(
        tenant_id,
        "anthropic",
        [{"resource_type": "workspace", "resource_id": "ws_mcs", "resource_name": "mcs-dev"}],
    )
    # ...and when it reappears, the mapping is still there.
    _register(tenant_id)
    assert resources.get_classifications(tenant_id, "anthropic")[("api_key", "k_a")] == "internal"


def test_set_classification_on_unregistered_resource_creates_it(tenant_id):
    resources.set_classification(
        tenant_id, "anthropic", "api_key", "k_new", "development", resource_name="new-key"
    )
    listed = {r["resource_id"]: r for r in resources.list_resources(tenant_id, "anthropic")}
    assert listed["k_new"]["classification"] == "development"
    assert listed["k_new"]["resource_name"] == "new-key"
