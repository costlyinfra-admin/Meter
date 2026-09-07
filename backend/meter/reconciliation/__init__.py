"""Provider invoice reconciliation — an additive, opt-in module.

Compares an official provider billing export against the spend Meter
already tracks, and explains the difference.

The module is deliberately sealed off from the rest of the product:

  * it owns its own tables (all named ``recon_*``) and writes only to those;
  * it reads existing cost data through one read-only interface (``tracked``),
    which issues SELECTs and nothing else;
  * it is off unless a tenant turns it on, and every route checks that first;
  * nothing outside it imports it, so no existing query, total or screen can
    depend on it, and a failure in here cannot reach ingestion, the Overview,
    Optimize or Alerts.

Removing the module means deleting this package, its migration's tables, its
router line in api.py and its routes in the web app. Nothing else refers to it.
"""
