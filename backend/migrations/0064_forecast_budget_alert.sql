-- 0064: an alert that fires on where the month is HEADING.
--
-- budget_pct compares actual month-to-date spend with the budget. By the time it
-- fires the money has been spent, and the most anyone can do is stop next month
-- going the same way. forecast_budget_pct compares the PROJECTED month-end spend
-- — the same projection the Forecast page and the Overview's budget panel show —
-- so it fires while there is still most of a month left to act in.
--
-- Named as a constraint change, the way 0050 did it, because the column's CHECK
-- is the thing standing between a rule the application accepts and a row the
-- database will store. The application-side validation (alerts.valid_conditions)
-- still decides which metrics and scopes may use it.
ALTER TABLE alert_rule DROP CONSTRAINT alert_rule_condition_type_check;
ALTER TABLE alert_rule ADD CONSTRAINT alert_rule_condition_type_check
    CHECK (condition_type IN (
        'exceeds', 'increase_pct', 'budget_pct', 'forecast_budget_pct', 'falls_below'
    ));
