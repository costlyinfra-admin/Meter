"""Products — the customer-defined grouping above features.

The things these tests exist to hold:

  * a person's assignment is never overwritten — not by re-applying the repo
    mapping, and not by a later discovery run,
  * a feature whose repositories belong to two products is Unassigned rather
    than guessed into one of them,
  * deleting a product leaves its features (and their cost) alone.
"""

from __future__ import annotations

import pytest
from meter import features, products
from meter.db import app_dsn, connect, tenant_tx


def _feature(conn, tenant_id, name, repos=()):
    """A feature with pull-request evidence in `repos`."""
    fid = conn.execute(
        "INSERT INTO feature (tenant_id, name, status, discovery_confidence) "
        "VALUES (%s, %s, 'confirmed', 'high') RETURNING id",
        (tenant_id, name),
    ).fetchone()[0]
    for i, repo in enumerate(repos):
        conn.execute(
            "INSERT INTO feature_signal (tenant_id, feature_id, signal_type, external_ref, "
            "confidence, source) VALUES (%s, %s, 'pr', %s, 'high', 'github')",
            (tenant_id, fid, f"{repo}#{i + 1}"),
        )
    conn.commit()
    return str(fid)


def _product_of(tenant_id, feature_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT product_id, product_source FROM feature WHERE id = %s", (feature_id,)
        ).fetchone()


class TestTheProductList:
    def test_creates_renames_and_lists_products(self, app_env, tenant_id):
        made = products.create_product(tenant_id, "Sentinel", "The detection product")
        assert made["name"] == "Sentinel"
        assert made["repos"] == [] and made["feature_count"] == 0

        products.rename_product(tenant_id, made["id"], name="Sentinel Platform")
        assert [p["name"] for p in products.list_products(tenant_id)] == ["Sentinel Platform"]

    def test_refuses_a_name_that_differs_only_in_case(self, app_env, tenant_id):
        products.create_product(tenant_id, "Sentinel")
        with pytest.raises(products.DuplicateProduct):
            products.create_product(tenant_id, "sentinel")

    def test_deleting_a_product_keeps_its_features(self, app_env, tenant_id):
        made = products.create_product(tenant_id, "Sentinel")
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/sentinel-api"])
        features.set_product(tenant_id, fid, made["id"])

        products.delete_product(tenant_id, made["id"])

        # The feature survives, and does not keep claiming a person assigned it
        # to a product that no longer exists.
        assert _product_of(tenant_id, fid) == (None, None)
        assert [f["name"] for f in features.list_features(tenant_id)] == ["Alert triage"]

    def test_unknown_product_is_not_found(self, app_env, tenant_id):
        with pytest.raises(products.ProductNotFound):
            products.rename_product(tenant_id, "00000000-0000-0000-0000-000000000000", name="x")


class TestMappingRepositories:
    def test_a_repo_moves_between_products_instead_of_erroring(self, app_env, tenant_id):
        a = products.create_product(tenant_id, "Sentinel")
        b = products.create_product(tenant_id, "Beacon")
        products.set_repos(tenant_id, a["id"], ["acme/shared"])

        # The customer correcting themselves must not hit a unique-constraint wall.
        moved = products.set_repos(tenant_id, b["id"], ["acme/shared"])
        assert moved["repos"] == ["acme/shared"]
        assert products.list_products(tenant_id)[1]["repos"] == []  # Sentinel, by name order

    def test_repo_names_are_matched_regardless_of_case(self, app_env, tenant_id):
        made = products.create_product(tenant_id, "Sentinel")
        products.set_repos(tenant_id, made["id"], ["Acme/Sentinel-API"])
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/sentinel-api"])

        products.reassign_from_repos(tenant_id)
        assert _product_of(tenant_id, fid)[0] is not None

    def test_suggests_products_from_repository_names(self, app_env, tenant_id):
        _feature(
            app_env,
            tenant_id,
            "Alert triage",
            ["acme/sentinel-api", "acme/sentinel-web", "acme/beacon-ui"],
        )
        suggested = products.suggest_from_repos(tenant_id)["suggestions"]
        # Grouped on the leading word, biggest group first — 20+ repos collapse
        # into a handful of candidates rather than a list to work through.
        assert suggested[0]["name"] == "Sentinel"
        assert suggested[0]["repos"] == ["acme/sentinel-api", "acme/sentinel-web"]


class TestDerivingAFeaturesProduct:
    def test_one_mapped_product_assigns_it(self, app_env, tenant_id):
        made = products.create_product(tenant_id, "Sentinel")
        products.set_repos(tenant_id, made["id"], ["acme/sentinel-api"])
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/sentinel-api"])

        assert products.reassign_from_repos(tenant_id)["assigned"] == 1
        assert _product_of(tenant_id, fid) == (
            __import__("uuid").UUID(made["id"]),
            "discovery",
        )

    def test_no_mapped_repo_leaves_it_unassigned(self, app_env, tenant_id):
        products.create_product(tenant_id, "Sentinel")
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/something-else"])

        assert products.reassign_from_repos(tenant_id)["unassigned"] == 1
        assert _product_of(tenant_id, fid) == (None, None)

    def test_a_feature_spanning_two_products_is_not_guessed(self, app_env, tenant_id):
        a = products.create_product(tenant_id, "Sentinel")
        b = products.create_product(tenant_id, "Beacon")
        products.set_repos(tenant_id, a["id"], ["acme/sentinel-api"])
        products.set_repos(tenant_id, b["id"], ["acme/beacon-ui"])
        fid = _feature(app_env, tenant_id, "Shared auth", ["acme/sentinel-api", "acme/beacon-ui"])

        result = products.reassign_from_repos(tenant_id)
        assert result["spanning"] == 1
        # A majority vote here would be a number the customer cannot check.
        assert _product_of(tenant_id, fid) == (None, None)

        listed = products.spanning_features(tenant_id)
        assert listed[0]["name"] == "Shared auth"
        assert listed[0]["products"] == ["Beacon", "Sentinel"]

    def test_derives_from_pull_requests_alone(self, app_env, tenant_id):
        """Features discovered before products existed carry only 'pr' signals."""
        made = products.create_product(tenant_id, "Sentinel")
        products.set_repos(tenant_id, made["id"], ["acme/sentinel-api"])
        fid = _feature(app_env, tenant_id, "Old feature", ["acme/sentinel-api"])

        products.reassign_from_repos(tenant_id)
        assert _product_of(tenant_id, fid)[1] == "discovery"


class TestAPersonsAssignmentWins:
    def test_re_applying_the_mapping_does_not_overwrite_it(self, app_env, tenant_id):
        a = products.create_product(tenant_id, "Sentinel")
        b = products.create_product(tenant_id, "Beacon")
        products.set_repos(tenant_id, a["id"], ["acme/sentinel-api"])
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/sentinel-api"])

        features.set_product(tenant_id, fid, b["id"])  # the repos say Sentinel
        products.reassign_from_repos(tenant_id)

        stored, source = _product_of(tenant_id, fid)
        assert str(stored) == b["id"] and source == "user"

    def test_clearing_it_hands_the_feature_back_to_the_mapping(self, app_env, tenant_id):
        a = products.create_product(tenant_id, "Sentinel")
        products.set_repos(tenant_id, a["id"], ["acme/sentinel-api"])
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/sentinel-api"])

        features.set_product(tenant_id, fid, None)
        assert _product_of(tenant_id, fid) == (None, None)

        products.reassign_from_repos(tenant_id)
        assert _product_of(tenant_id, fid)[1] == "discovery"

    def test_assigning_to_a_product_that_does_not_exist_is_refused(self, app_env, tenant_id):
        fid = _feature(app_env, tenant_id, "Alert triage")
        with pytest.raises(products.ProductNotFound):
            features.set_product(tenant_id, fid, "00000000-0000-0000-0000-000000000000")

    def test_splitting_a_feature_keeps_both_halves_in_its_product(self, app_env, tenant_id):
        made = products.create_product(tenant_id, "Sentinel")
        fid = _feature(app_env, tenant_id, "Alert triage", ["acme/sentinel-api"])
        features.set_product(tenant_id, fid, made["id"])

        halves = features.split_feature(tenant_id, fid, [{"name": "Triage"}, {"name": "Ranking"}])
        assert [h["product_name"] for h in halves] == ["Sentinel", "Sentinel"]
        # And the split does not quietly downgrade a person's decision to a guess.
        assert [h["product_source"] for h in halves] == ["user", "user"]
