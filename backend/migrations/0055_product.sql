-- 0055: product — the customer-defined grouping ABOVE feature.
--
-- A company with one GitHub org can still sell several products. Discovery finds
-- features across every repo in scope, but nothing sat above a feature, so
-- "what does each product cost us?" had no answer.
--
--   product       — the customer's own name for a thing they sell. The rollup key.
--   product_repo  — which repositories belong to it. This is the customer's
--                   mental model ("a product is a set of repos") and is used ONLY
--                   to derive a feature's product, never to hold cost.
--   feature.product_id / product_source — the spine link, exactly mirroring
--                   category / category_source: a person's assignment is a stored
--                   fact that a later discovery run never overwrites.
--
-- WHY NOT ai_application. 0049's ai_application groups TRACES: it is created
-- automatically from the SDK's application slug and covers only instrumented
-- inference. Build cost and connector inference never reach it, so a rollup built
-- on it would be incomplete by construction. Product sits on the feature spine
-- instead, where every kind of cost already attributes.
--
-- WHY COST TABLES ARE UNTOUCHED. Cost attributes to feature_id and reaches a
-- product by join. A product_id on build_cost/inference_cost would be a second
-- attribution path that could disagree with the first.

CREATE TABLE product (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    description text CHECK (description IS NULL OR length(description) <= 500),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- "Sentinel" and "sentinel" are one product, not two.
CREATE UNIQUE INDEX product_name_idx ON product (tenant_id, lower(name));

CREATE TABLE product_repo (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    product_id  uuid NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    -- "owner/name", stored lowercase: GitHub treats repository names
    -- case-insensitively and discovery already matches scope that way.
    repo        text NOT NULL CHECK (length(repo) BETWEEN 1 AND 300),
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- One repo belongs to at most one product. The derivation rule depends on
    -- this: it is what makes a feature's product unambiguous.
    UNIQUE (tenant_id, repo)
);

CREATE INDEX product_repo_product_idx ON product_repo (tenant_id, product_id);

-- ON DELETE SET NULL: deleting a product must never delete features or their
-- cost. products.delete_product clears product_source in the same transaction,
-- which the constraint cannot do on its own.
ALTER TABLE feature ADD COLUMN product_id uuid REFERENCES product(id) ON DELETE SET NULL;
ALTER TABLE feature ADD COLUMN product_source text
    CHECK (product_source IN ('discovery', 'user'));

CREATE INDEX feature_product_idx ON feature (tenant_id, product_id);

-- Discovery now records which repositories a feature's pull requests came from,
-- as 'repo' signals. That evidence is what the product badge points at, and it
-- is read back per feature when the repo mapping changes.
CREATE INDEX feature_signal_repo_idx
    ON feature_signal (tenant_id, external_ref) WHERE signal_type = 'repo';

GRANT SELECT, INSERT, UPDATE, DELETE ON product TO meter_app;
ALTER TABLE product ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON product
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON product_repo TO meter_app;
ALTER TABLE product_repo ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON product_repo
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

COMMENT ON TABLE product IS
    'A thing the customer sells. Groups features and rolls up their build and '
    'inference cost. Not to be confused with ai_application, which is an '
    'instrumented service as the SDK names it.';
COMMENT ON COLUMN feature.product_source IS
    '''user'' when a person assigned it (never overwritten), ''discovery'' when '
    'it was derived from the repositories the feature''s pull requests are in.';

-- 0049 called this "the product surface a workflow belongs to", which now reads
-- as a second product concept. The table is unchanged; only the words are.
COMMENT ON TABLE ai_application IS
    'An instrumented service or agent, as the SDK or OTel names it. NOT the '
    'customer''s product — see the product table, which groups features and cost.';
