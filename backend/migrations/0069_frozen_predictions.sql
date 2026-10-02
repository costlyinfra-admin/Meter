-- 0069: what Meter predicted when a change was applied, frozen (EX-5).
--
-- optimization_action already freezes the dollar projection, as the browser
-- sent it. That says how much, but not how sure Meter was, what the figure
-- rested on, or what it implied for the bill — so once the change ships there
-- was nothing to hold the bill against except the projection itself.
--
-- Now, when a change is marked applied, Meter records its own figure for it
-- (computed on the server, the one the card showed), what kind of figure it
-- was — measured, tested, or a ceiling — how confident, which test backed it,
-- and the feature's spend in that period. The saving as a share of that spend
-- is the prediction the bill can check: the cost of a unit of the feature's
-- work should fall by that much. Optimization reads the bill's cost per unit
-- afterwards and reports how much of the predicted fall arrived — per change,
-- and across changes, by kind of figure. That is Meter's calibration: how far
-- its own claims can be trusted.
--
-- Rows applied before this migration have no prediction and say so; nothing
-- is back-filled, because a prediction reconstructed later is not one that was
-- made at the time.

ALTER TABLE optimization_action
    ADD COLUMN predicted_savings_type text
        CHECK (predicted_savings_type IS NULL OR predicted_savings_type IN
               ('measured', 'tested', 'modeled_ceiling', 'directional')),
    ADD COLUMN predicted_confidence text
        CHECK (predicted_confidence IS NULL OR predicted_confidence IN ('high', 'med', 'low')),
    -- How the figure was tested, if it was (experiments.annotate's validation).
    ADD COLUMN predicted_validation text
        CHECK (predicted_validation IS NULL OR predicted_validation IN
               ('untested', 'simulated', 'tested_offline', 'tested_live', 'failed',
                'inconclusive')),
    ADD COLUMN experiment_id uuid REFERENCES experiment(id) ON DELETE SET NULL,
    -- The feature's spend in the applied period, on the reconciled basis.
    ADD COLUMN predicted_spend numeric(14, 4) CHECK (predicted_spend IS NULL OR predicted_spend >= 0),
    -- The saving as a share of that spend: how far a unit of work should get cheaper.
    ADD COLUMN predicted_reduction numeric(7, 6)
        CHECK (predicted_reduction IS NULL OR predicted_reduction BETWEEN 0 AND 1),
    ADD COLUMN predicted_at timestamptz;
