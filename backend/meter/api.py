"""Meter HTTP API (FastAPI).

Auth is cookie-session based: a signed, http-only session cookie (Starlette
SessionMiddleware) holds the user id. Tenant-scoped data is always read/written
under the authenticated user's tenant, which drives RLS.

Run locally:  uvicorn --factory meter.api:create_app --reload
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Annotated, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import (
    __version__,
    admin,
    alerts,
    alerts_eval,
    assistant,
    auth,
    budgets,
    build,
    claudecode,
    compute,
    cost_sources,
    credentials,
    cursorspend,
    dashboard,
    discovery,
    discovery_llm,
    entra,
    features,
    hook,
    inference,
    okta,
    optimize_measured,
    resources,
    seats,
    settings,
)
from .github import GitHubError
from .providers import ProviderError
from .reconciliation import api as reconciliation_api

logger = logging.getLogger("meter.api")

#: Where "Contact support" in the assistant writes to. Overridable so a fork, or
#: a customer running their own deployment, can point it at their own desk.
SUPPORT_EMAIL = os.environ.get("METER_SUPPORT_EMAIL", "support@costlyinfra.com")

#: The named review periods every window-scoped endpoint accepts.
_RANGE_RE = "^(this_month|last_month|last_3_months|last_6_months|last_12_months)$"

# Quick-start alert templates — prefill the create form but stay fully editable.
# The two budget_pct templates carry no budget of their own: the denominator is
# the organization's configured budget, and a template that shipped a number
# would be exactly the invented figure this feature exists to remove. The
# `requires_budget` flag lets the form say so before the save is attempted.
_ALERT_TEMPLATES = [
    {
        "id": "monthly_budget",
        "label": "Monthly AI spend exceeds budget",
        "requires_budget": True,
        "rule": {
            "name": "Monthly AI spend over budget",
            "metric": "combined_cost",
            "scope_type": "organization",
            "condition_type": "budget_pct",
            "threshold": 100,
            "window": "monthly",
            "cooldown": "day",
        },
    },
    {
        "id": "daily_spike",
        "label": "Daily inference cost spikes by 30%",
        "rule": {
            "name": "Daily inference cost spike",
            "metric": "inference_cost",
            "scope_type": "organization",
            "condition_type": "increase_pct",
            "threshold": 30,
            "window": "daily",
            "cooldown": "day",
        },
    },
    {
        "id": "unattributed",
        "label": "Unattributed spend exceeds 10% of total",
        "requires_budget": True,
        "rule": {
            "name": "High unattributed spend",
            "metric": "unattributed_cost",
            "scope_type": "organization",
            "condition_type": "budget_pct",
            "threshold": 10,
            "window": "monthly",
            "cooldown": "week",
        },
    },
    {
        "id": "feature_cpu",
        "label": "Feature cost per active user exceeds a threshold",
        "rule": {
            "name": "Feature cost/user too high",
            "metric": "cost_per_user",
            "scope_type": "feature",
            "condition_type": "exceeds",
            "threshold": 50,
            "window": "monthly",
            "cooldown": "week",
        },
    },
    {
        "id": "provider_spend",
        "label": "Provider spend exceeds a threshold",
        "rule": {
            "name": "Provider spend threshold",
            "metric": "inference_cost",
            "scope_type": "provider",
            "condition_type": "exceeds",
            "threshold": 1000,
            "window": "monthly",
            "cooldown": "day",
        },
    },
    {
        "id": "token_usage",
        "label": "Model token usage exceeds a threshold",
        "rule": {
            "name": "Model token usage threshold",
            "metric": "token_usage",
            "scope_type": "model",
            "condition_type": "exceeds",
            "threshold": 100000000,
            "window": "monthly",
            "cooldown": "day",
        },
    },
]


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH, max_length=1024)


class LoginRequest(BaseModel):
    email: str
    password: str


class AlertChannel(BaseModel):
    channel: str = Field(min_length=1, max_length=16)
    target: Optional[str] = Field(default=None, max_length=2048)
    secret: Optional[str] = Field(default=None, max_length=2048)


class AlertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    metric: str = Field(min_length=1, max_length=32)
    scope_type: str = Field(default="organization", max_length=16)
    scope_ref: Optional[str] = Field(default=None, max_length=256)
    condition_type: str = Field(min_length=1, max_length=16)
    threshold: float
    budget_amount: Optional[float] = None
    window: str = Field(min_length=1, max_length=16)
    cooldown: str = Field(default="day", max_length=16)
    recovery_notify: bool = True
    enabled: bool = True
    channels: list[AlertChannel] = Field(default_factory=list)


class AlertEnableRequest(BaseModel):
    enabled: bool


class MarkReadRequest(BaseModel):
    event_ids: list[str] = Field(default_factory=list)


class ClassifyRequest(BaseModel):
    resource_type: str = Field(min_length=1, max_length=32)
    resource_id: str = Field(min_length=1, max_length=256)
    classification: str = Field(min_length=1, max_length=16)
    resource_name: Optional[str] = Field(default=None, max_length=256)


class SettingsRequest(BaseModel):
    # All optional: a PATCH updates only the fields that are present. The generous
    # length cap just bounds the payload; the precise limit is enforced (as HTTP 400)
    # in settings.update_settings so all validation errors come back consistently.
    org_name: Optional[str] = Field(default=None, max_length=1000)
    timezone: Optional[str] = Field(default=None, max_length=64)
    currency: Optional[str] = Field(default=None, max_length=8)
    customer_id_storage: Optional[str] = Field(default=None, max_length=16)
    store_prompts: Optional[bool] = None
    data_retention: Optional[str] = Field(default=None, max_length=16)


class BudgetRequest(BaseModel):
    # Every field required: a budget is one coherent statement, not a set of
    # independently patchable parts, and a half-set budget has no meaning.
    amount: float = Field(gt=0)
    cadence: str = Field(max_length=16)
    effective_from: str = Field(max_length=10)
    currency: Optional[str] = Field(default=None, max_length=8)


class CredentialRequest(BaseModel):
    secret: str = Field(min_length=1, max_length=8192)
    label: Optional[str] = Field(default=None, max_length=200)


class DiscoveryRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=200)  # GitHub org or user
    days: int = Field(default=90, ge=1, le=365)
    # Earliest merge date to fetch. Wins over `days` when given, so a caller can
    # say "cover March" without converting a window into a lookback against a
    # clock it cannot see. YYYY-MM-DD.
    since: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Selected "owner/name" repos to analyze; empty = the whole org (legacy behavior).
    repos: list[str] = Field(default_factory=list, max_length=200)


class DiscoveryScheduleRequest(BaseModel):
    enabled: bool
    lookback_days: Optional[int] = Field(default=None, ge=1, le=365)


class AddFeatureRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class RenameFeatureRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class SplitGroup(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    signal_ids: list[str] = Field(default_factory=list)
    description: Optional[str] = Field(default=None, max_length=2000)


class SplitRequest(BaseModel):
    groups: list[SplitGroup] = Field(min_length=1)


class MergeRequest(BaseModel):
    feature_ids: list[str] = Field(min_length=2)
    name: Optional[str] = Field(default=None, max_length=200)


class ConfirmRequest(BaseModel):
    feature_ids: Optional[list[str]] = None


class SignalRequest(BaseModel):
    signal_type: str = Field(min_length=1, max_length=20)
    external_ref: str = Field(min_length=1, max_length=300)


class IngestRequest(BaseModel):
    provider: str = Field(
        pattern=(
            "^(anthropic|openai|google|openrouter|together|fireworks|bedrock"
            "|azure|litellm|vercel|modal|elevenlabs"
            "|groq|mistral|xai|perplexity|cohere|replicate|portkey|helicone)$"
        )
    )
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")  # YYYY-MM
    # How many months to pull, ending at `period` (or this month). 1 = single month
    # (nightly refresh); >1 backfills history (the manual "Sync now" pulls 12).
    months: int = Field(default=1, ge=1, le=24)


class BuildImportRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=5_000_000)
    tool: Optional[str] = Field(default=None, max_length=20)
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class UsageRequest(BaseModel):
    active_users: int = Field(ge=0)
    events: Optional[int] = Field(default=None, ge=0)
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class CategoryRequest(BaseModel):
    # null clears the tag and hands the feature back to the discovery guess.
    category: Optional[
        Literal["chat", "api", "ui", "docs", "data", "auth", "reporting", "integration", "infra"]
    ] = None


class HookSignal(BaseModel):
    """Optional optimization signal on a metered event (opt spec §6, SDK v0.3).

    Carries only hashes and counts — never prompt or response text.
    """

    kind: str = Field(pattern=r"^(duplicate|prefix)$")
    fingerprint: str = Field(min_length=1, max_length=128)
    count: int = Field(default=1, ge=0)
    prefix_tokens: Optional[int] = Field(default=None, ge=0)
    cached_count: int = Field(default=0, ge=0)
    tokens_in: Optional[int] = Field(default=None, ge=0)
    tokens_out: Optional[int] = Field(default=None, ge=0)


class HookEvent(BaseModel):
    provider: str
    model: str = ""
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    feature_id: Optional[str] = None
    occurred_at: Optional[str] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)  # SDK v0.2 (optional)
    metadata: Optional[dict] = None  # e.g. {"customer_id": "..."}
    signal: Optional[HookSignal] = None  # SDK optimize mode (opt spec)


class HookEventsRequest(BaseModel):
    events: list[HookEvent] = Field(min_length=1, max_length=10000)
    # Stable across retries of the same batch, so re-delivery applies nothing.
    # Optional: pre-0.4 SDKs don't send one and every call is applied, as before.
    batch_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


class AssistantPassage(BaseModel):
    """One handbook excerpt the browser retrieved for this question.

    The knowledge base ships in the app bundle and retrieval runs there, so the
    excerpts arrive with the question rather than from a second copy on the
    server that could drift out of date. See meter/assistant.py.
    """

    id: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=assistant.MAX_PASSAGE_CHARS)


class AssistantTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=assistant.MAX_HISTORY_CHARS)


class AssistantRequest(BaseModel):
    question: str = Field(min_length=1, max_length=assistant.MAX_QUESTION)
    passages: list[AssistantPassage] = Field(
        default_factory=list, max_length=assistant.MAX_PASSAGES
    )
    history: list[AssistantTurn] = Field(default_factory=list, max_length=assistant.MAX_HISTORY)
    #: Where the user is in the app, so the answer can be about the screen in
    #: front of them. A label, not a URL with data in it.
    page: str = Field(default="", max_length=80)


class DiscoveryLlmRequest(BaseModel):
    """BYOK for feature discovery. `api_key` is write-only and never read back."""

    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    enabled: bool = True


class DiscoveryLlmTestRequest(BaseModel):
    """Test a configuration. With no api_key, tests the one already stored."""

    provider: Optional[str] = Field(default=None, max_length=40)
    base_url: str = Field(default="", max_length=500)
    model: Optional[str] = Field(default=None, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=4096)


class DiscoveryLlmEnabledRequest(BaseModel):
    enabled: bool


class ReconcileRequest(BaseModel):
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")


# The measured levers that can be marked applied (opt spec §18).
_LEVER_PATTERN = r"^(duplicate_calls|prompt_caching|provider_switch|model_rightsizing)$"


class ApplyOpportunityRequest(BaseModel):
    lever: str = Field(pattern=_LEVER_PATTERN)
    projected_monthly: float = Field(ge=0)


class AdminConnectorRequest(BaseModel):
    connector_type: str = Field(min_length=1, max_length=40)
    secret: str = Field(min_length=1, max_length=8192)
    label: Optional[str] = Field(default=None, max_length=200)


class CopilotSyncRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=120)
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class SeatSourceRequest(BaseModel):
    provider: str = Field(default="okta", pattern="^(okta|entra)$")
    app_id: str = Field(min_length=1, max_length=120)
    app_label: str = Field(default="", max_length=120)
    tool: str = Field(min_length=1, max_length=60)
    plan: str = Field(min_length=1, max_length=60)


class SeatSyncRequest(BaseModel):
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class TrainingCostRequest(BaseModel):
    feature_id: str
    amount: float = Field(ge=0)
    label: str = Field(min_length=1, max_length=120)
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    run_ref: Optional[str] = None


class ComputePoolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider_label: str = Field(min_length=1, max_length=60)
    monthly_cost: float = Field(ge=0)


class AllocateRequest(BaseModel):
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    pool_id: Optional[str] = None


def _parse_period(value: Optional[str]) -> dt.date:
    if not value:
        today = dt.date.today()
        return today.replace(day=1)
    year, month = value.split("-")
    return dt.date(int(year), int(month), 1)


def _real_user(request: Request) -> auth.User:
    """The actually-logged-in user (ignores any admin impersonation)."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = auth.get_user(user_id)
    if user is None:  # session points at a deleted user
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def _raise_github(exc: GitHubError):
    """Map a GitHubError to a user-facing HTTP error (shared by discovery routes)."""
    if exc.status == 401:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub rejected the token. Reconnect with a valid token.",
        ) from exc
    if exc.status in (403, 404):  # rate limit, or owner not found / no public repos
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail=f"GitHub error: {exc}"
    ) from exc


def _current_user(request: Request) -> auth.User:
    """The effective user for tenant-scoped endpoints. If an admin is impersonating
    a customer, the tenant_id is swapped to that customer's — the entire customer UI
    then operates in that tenant with zero duplication."""
    user = _real_user(request)
    target = request.session.get("impersonate_tenant")
    if target and admin.is_admin(user["email"]):
        return {**user, "tenant_id": target}
    return user


def _admin_user(request: Request) -> auth.User:
    """Gate for the internal admin portal — a real, allow-listed admin user."""
    user = _real_user(request)
    if not admin.is_admin(user["email"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


# The authenticated user, resolved from the session cookie (FastAPI dependency).
CurrentUser = Annotated[auth.User, Depends(_current_user)]
AdminUser = Annotated[auth.User, Depends(_admin_user)]
RealUser = Annotated[auth.User, Depends(_real_user)]


def create_app() -> FastAPI:
    secret_key = os.environ.get("APP_SECRET_KEY")
    if not secret_key:
        raise RuntimeError("APP_SECRET_KEY must be set to run the API.")

    logging.basicConfig(
        level=os.environ.get("METER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Secure cookies behind HTTPS in production (set METER_SECURE_COOKIES=true).
    secure_cookies = os.environ.get("METER_SECURE_COOKIES", "false").lower() in (
        "1",
        "true",
        "yes",
    )

    app = FastAPI(title="Meter API", version=__version__)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        same_site="lax",
        https_only=secure_cookies,
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error: %s %s", request.method, request.url.path)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %d (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.post("/api/auth/signup")
    def signup(body: SignupRequest, request: Request) -> auth.User:
        try:
            user = auth.signup(body.email, body.password)
        except auth.EmailAlreadyRegistered as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        request.session["user_id"] = user["id"]
        return user

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request) -> auth.User:
        user = auth.login(body.email, body.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
            )
        request.session["user_id"] = user["id"]
        return user

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/auth/me")
    def me(request: Request, user: CurrentUser) -> dict:
        real = _real_user(request)
        is_admin = admin.is_admin(real["email"])
        impersonating = None
        target = request.session.get("impersonate_tenant")
        if target and is_admin:
            impersonating = {"tenant_id": target, "company": admin.company_name(target)}
        return {
            **user,
            "is_admin": is_admin,
            "impersonating": impersonating,
            # Organization name of the effective tenant (shown in the UI header).
            "org_name": admin.company_name(user["tenant_id"]),
        }

    # ---- Organization settings (administrative Settings page) -----------
    @app.get("/api/settings")
    def get_settings(user: CurrentUser) -> dict:
        return settings.get_settings(user["tenant_id"])

    @app.patch("/api/settings")
    def update_settings(body: SettingsRequest, user: CurrentUser) -> dict:
        # Only send through the fields the client actually set (PATCH semantics).
        changes = body.model_dump(exclude_unset=True)
        try:
            return settings.update_settings(user["tenant_id"], changes)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # ---- Budget (Settings -> Budgets) -----------------------------------
    # Tenant-scoped like the rest of Settings: every member of an organization
    # reads and writes the same budget, through their own tenant's RLS context.
    @app.get("/api/budget")
    def get_budget(user: CurrentUser) -> dict:
        return {"budget": budgets.get_budget(user["tenant_id"])}

    @app.put("/api/budget")
    def put_budget(body: BudgetRequest, user: CurrentUser) -> dict:
        try:
            saved = budgets.set_budget(
                user["tenant_id"], body.model_dump(), actor=user.get("email")
            )
        except budgets.BudgetError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {"budget": saved}

    @app.delete("/api/budget")
    def delete_budget(user: CurrentUser) -> dict:
        budgets.remove_budget(user["tenant_id"])
        return {"budget": None}

    @app.get("/api/budget/forecast")
    def budget_forecast(
        user: CurrentUser,
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        tenant_id = user["tenant_id"]
        s = _parse_period(start) if start else None
        e = _parse_period(end) if end else None
        win_start, win_end = dashboard.resolve_window(tenant_id, s, e, range)
        # The same figure the Overview's "Potential savings" card shows: measured
        # plus modelled, never the directional ones, which carry no defensible
        # number. Read here rather than passed in so the forecast cannot be
        # handed a savings figure the rest of the product would not agree with.
        try:
            overview = optimize_measured.copilot_overview(tenant_id, win_end)
            savings = overview["totals"]["measured"] + overview["totals"]["modeled_ceiling"]
        except Exception:  # noqa: BLE001 - a slow/failing Optimize must not take the card down
            savings = None
        return budgets.period_forecast(tenant_id, win_start, win_end, identified_savings=savings)

    @app.get("/api/connectors")
    def list_connectors(user: CurrentUser) -> list[credentials.ConnectorStatus]:
        return credentials.connector_statuses(user["tenant_id"])

    @app.post("/api/connectors/{connector_type}/credential", status_code=status.HTTP_204_NO_CONTENT)
    def save_connector_credential(
        connector_type: str,
        body: CredentialRequest,
        user: CurrentUser,
    ) -> Response:
        try:
            credentials.save_credential(user["tenant_id"], connector_type, body.secret, body.label)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- Feature discovery + editing (wizard step 2) --------------------
    @app.get("/api/discovery/repos")
    def discovery_repos(
        user: CurrentUser, owner: str = Query(min_length=1, max_length=200)
    ) -> dict:
        # List an org's repositories so the user can pick which to analyze (before
        # running discovery). Token optional (public orgs work unauthenticated).
        token = credentials.get_secret(user["tenant_id"], "github")
        try:
            return {"owner": owner, "repos": discovery.list_repos(owner, token)}
        except GitHubError as exc:
            _raise_github(exc)

    @app.get("/api/discovery/scope")
    def discovery_scope(user: CurrentUser) -> dict:
        # The last-used org + selected repos, to prefill the selector.
        return discovery.get_scope(user["tenant_id"]) or {"owner": None, "repos": []}

    @app.post("/api/discovery/run")
    def run_discovery(body: DiscoveryRequest, user: CurrentUser) -> dict:
        # Token is optional: without a connected GitHub credential, discovery
        # analyzes PUBLIC organizations/repos via GitHub's unauthenticated API.
        token = credentials.get_secret(user["tenant_id"], "github")
        try:
            return discovery.run_discovery(
                user["tenant_id"],
                body.owner,
                token,
                days=body.days,
                since=dt.date.fromisoformat(body.since) if body.since else None,
                repos=body.repos,
                started_by=user.get("email"),
            )
        except GitHubError as exc:
            _raise_github(exc)

    @app.get("/api/discovery/runs")
    def discovery_runs(user: CurrentUser) -> dict:
        """What discovery has done, and therefore what it has covered."""
        return {
            "runs": discovery.run_history(user["tenant_id"]),
            "coverage": discovery.coverage(user["tenant_id"]),
        }

    @app.get("/api/discovery/schedule")
    def get_discovery_schedule(user: CurrentUser) -> dict:
        return discovery.get_schedule(user["tenant_id"])

    @app.put("/api/discovery/schedule")
    def put_discovery_schedule(body: DiscoveryScheduleRequest, user: CurrentUser) -> dict:
        try:
            return discovery.set_schedule(
                user["tenant_id"], enabled=body.enabled, lookback_days=body.lookback_days
            )
        except discovery.ScheduleError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/api/features")
    def list_features(
        user: CurrentUser,
        status_filter: Optional[str] = Query(default=None, alias="status"),
    ) -> list[dict]:
        return features.list_features(user["tenant_id"], status=status_filter)

    @app.post("/api/features", status_code=status.HTTP_201_CREATED)
    def add_feature(body: AddFeatureRequest, user: CurrentUser) -> dict:
        return features.add_feature(user["tenant_id"], body.name, body.description)

    @app.patch("/api/features/{feature_id}")
    def rename_feature(feature_id: str, body: RenameFeatureRequest, user: CurrentUser) -> dict:
        try:
            return features.rename_feature(
                user["tenant_id"], feature_id, name=body.name, description=body.description
            )
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc

    @app.delete("/api/features/{feature_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_feature(feature_id: str, user: CurrentUser) -> Response:
        try:
            features.delete_feature(user["tenant_id"], feature_id)
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/features/{feature_id}/split")
    def split_feature(feature_id: str, body: SplitRequest, user: CurrentUser) -> list[dict]:
        groups = [g.model_dump() for g in body.groups]
        try:
            return features.split_feature(user["tenant_id"], feature_id, groups)
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc

    @app.post("/api/features/merge")
    def merge_features(body: MergeRequest, user: CurrentUser) -> dict:
        try:
            return features.merge_features(user["tenant_id"], body.feature_ids, name=body.name)
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/onboarding/confirm")
    def confirm_onboarding(body: ConfirmRequest, user: CurrentUser) -> list[dict]:
        return features.confirm_features(user["tenant_id"], body.feature_ids)

    @app.post("/api/features/{feature_id}/signals")
    def add_feature_signal(feature_id: str, body: SignalRequest, user: CurrentUser) -> dict:
        try:
            return features.add_signal(
                user["tenant_id"], feature_id, body.signal_type, body.external_ref
            )
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # ---- Inference cost ingest (connector path, M4) --------------------
    @app.post("/api/inference/ingest")
    def ingest_inference(body: IngestRequest, user: CurrentUser) -> dict:
        admin_key = credentials.get_secret(user["tenant_id"], body.provider)
        if not admin_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Connect {body.provider} before ingesting inference cost.",
            )
        period = _parse_period(body.period)
        try:
            if body.months > 1:
                return inference.run_inference_backfill(
                    user["tenant_id"],
                    body.provider,
                    admin_key,
                    months=body.months,
                    anchor=period,
                )
            return inference.run_inference_ingest(
                user["tenant_id"], body.provider, period, admin_key
            )
        except ProviderError as exc:
            if exc.status == 401:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{body.provider} rejected the admin key. Reconnect it.",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Provider error: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # Unreachable host / network failure — common for self-hosted gateway
            # URLs (LiteLLM, Vercel/Modal overrides). Surface it cleanly, not a 500.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not reach {body.provider}. Check the URL/credentials and retry.",
            ) from exc

    @app.get("/api/inference/summary")
    def inference_summary(
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        return inference.inference_summary(user["tenant_id"], _parse_period(period))

    # ---- Cost-source resource detail + classification (shared across providers) ----
    @app.get("/api/cost-sources/{provider}/detail")
    def cost_source_detail(
        provider: str,
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        # The single detail table shown inline under a source: its attributable
        # resources with their current manual classification and cost.
        resolved = _parse_period(period) if period else None
        return cost_sources.resource_detail(user["tenant_id"], provider, resolved)

    @app.post("/api/cost-sources/{provider}/classify")
    def classify_resource(provider: str, body: ClassifyRequest, user: CurrentUser) -> dict:
        try:
            return resources.set_classification(
                user["tenant_id"],
                provider,
                body.resource_type,
                body.resource_id,
                body.classification,
                resource_name=body.resource_name,
                updated_by=user["email"],
            )
        except resources.ResourceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # ---- Alerts (cost monitoring) --------------------------------------
    # Every route is tenant-scoped through CurrentUser + RLS, so a user can only
    # ever touch their own organization's rules/events/destinations.
    @app.get("/api/alerts/meta")
    def alerts_meta(user: CurrentUser) -> dict:
        # Vocabulary + valid combinations + quick-start templates for the form.
        return {
            "metrics": [{"value": m, "label": alerts.METRIC_LABELS[m]} for m in alerts.METRICS],
            "scopes": list(alerts.SCOPES),
            "conditions": list(alerts.CONDITIONS),
            "windows": list(alerts.WINDOWS),
            "cooldowns": list(alerts.COOLDOWNS),
            "channels": list(alerts.CHANNELS),
            "valid_conditions": {m: list(alerts.valid_conditions(m)) for m in alerts.METRICS},
            "valid_scopes": {m: list(alerts.valid_scopes(m)) for m in alerts.METRICS},
            "templates": _ALERT_TEMPLATES,
            # Whether a budget-percentage rule can be saved at all right now, and
            # what to say if it cannot. The form asks once rather than each
            # keystroke guessing at what the server will accept.
            "has_budget": budgets.get_budget(user["tenant_id"]) is not None,
            "budget_required_message": alerts.NO_BUDGET_MESSAGE,
            "budget_conditions": ["budget_pct"],
        }

    @app.get("/api/alerts")
    def list_alerts(user: CurrentUser) -> dict:
        return {
            "rules": alerts.list_rules(user["tenant_id"]),
            "summary": alerts.summary_counts(user["tenant_id"]),
        }

    @app.get("/api/alerts/summary")
    def alerts_summary(user: CurrentUser) -> dict:
        return alerts.summary_counts(user["tenant_id"])

    @app.get("/api/alerts/activity")
    def alerts_activity(user: CurrentUser) -> dict:
        return {"events": alerts.list_activity(user["tenant_id"])}

    @app.post("/api/alerts/activity/read")
    def alerts_mark_read(body: MarkReadRequest, user: CurrentUser) -> dict:
        return {"marked": alerts.mark_read(user["tenant_id"], body.event_ids)}

    @app.post("/api/alerts/activity/read-all")
    def alerts_mark_all_read(user: CurrentUser) -> dict:
        return {"marked": alerts.mark_all_read(user["tenant_id"])}

    @app.post("/api/alerts", status_code=status.HTTP_201_CREATED)
    def create_alert(body: AlertRequest, user: CurrentUser) -> dict:
        try:
            return alerts.create_rule(
                user["tenant_id"], body.model_dump(), created_by=user["email"]
            )
        except alerts.AlertError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/api/alerts/{alert_id}")
    def get_alert(alert_id: str, user: CurrentUser) -> dict:
        rule = alerts.get_rule(user["tenant_id"], alert_id)
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
        return {**rule, "history": alerts.rule_events(user["tenant_id"], alert_id)}

    @app.put("/api/alerts/{alert_id}")
    def update_alert(alert_id: str, body: AlertRequest, user: CurrentUser) -> dict:
        try:
            rule = alerts.update_rule(user["tenant_id"], alert_id, body.model_dump())
        except alerts.AlertError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
        return rule

    @app.post("/api/alerts/{alert_id}/enable")
    def enable_alert(alert_id: str, body: AlertEnableRequest, user: CurrentUser) -> dict:
        rule = alerts.set_enabled(user["tenant_id"], alert_id, body.enabled)
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
        return rule

    @app.post("/api/alerts/{alert_id}/duplicate", status_code=status.HTTP_201_CREATED)
    def duplicate_alert(alert_id: str, user: CurrentUser) -> dict:
        rule = alerts.duplicate_rule(user["tenant_id"], alert_id, created_by=user["email"])
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
        return rule

    @app.post("/api/alerts/{alert_id}/test")
    def test_alert(alert_id: str, user: CurrentUser) -> dict:
        result = alerts_eval.send_test(user["tenant_id"], alert_id)
        if not result.get("ok"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
        return result

    @app.delete("/api/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_alert(alert_id: str, user: CurrentUser) -> Response:
        if not alerts.delete_rule(user["tenant_id"], alert_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- Metering hook (M7) --------------------------------------------
    @app.post("/api/hook/token")
    def create_hook_token(user: CurrentUser) -> dict:
        # Returned once; only its hash is stored.
        return {"token": hook.generate_token(user["tenant_id"])}

    def _ingest_tenant(request: Request) -> str:
        # Hook requests authenticate with the per-tenant ingest token, not the
        # session cookie. Resolve it or 401.
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else header
        tenant_id = hook.resolve_tenant(token) if token else None
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingest token"
            )
        return tenant_id

    @app.post("/api/hook/events")
    def ingest_hook_events(body: HookEventsRequest, request: Request) -> dict:
        tenant_id = _ingest_tenant(request)
        return hook.ingest_events(
            tenant_id, [e.model_dump() for e in body.events], batch_id=body.batch_id
        )

    @app.get("/api/hook/salt")
    def hook_salt(request: Request) -> dict:
        # The SDK's optimize mode fetches its per-tenant fingerprint salt once.
        tenant_id = _ingest_tenant(request)
        return {"salt": hook.get_or_create_salt(tenant_id)}

    @app.post("/api/inference/reconcile")
    def reconcile_inference(body: ReconcileRequest, user: CurrentUser) -> list[dict]:
        return hook.reconcile(user["tenant_id"], _parse_period(body.period))

    # ---- Build cost ingest (coding tools, M5) --------------------------
    @app.post("/api/build/import")
    def import_build_cost(body: BuildImportRequest, user: CurrentUser) -> dict:
        period = _parse_period(body.period)
        try:
            spends = build.parse_csv(body.csv, default_tool=body.tool, default_period=period)
        except build.CsvImportError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return build.allocate_and_store(user["tenant_id"], spends, period)

    @app.post("/api/inference/refresh")
    def refresh_inference(user: CurrentUser) -> dict:
        """Pull the CURRENT month from every connected inference provider.

        Backs the Overview's refresh control, so it updates real cost instead of
        only re-reading stored rows. Single-month for speed; per-provider failures
        come back in `errors` rather than failing the whole refresh.
        """
        return inference.sync_connected(user["tenant_id"])

    @app.get("/api/build/summary")
    def build_summary(
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        return build.build_summary(user["tenant_id"], _parse_period(period))

    @app.post("/api/build/copilot/sync")
    def sync_copilot_seats(body: CopilotSyncRequest, user: CurrentUser) -> dict:
        token = credentials.get_secret(user["tenant_id"], "github")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connect GitHub to sync Copilot seats.",
            )
        try:
            return build.import_copilot_seats(
                user["tenant_id"], body.owner, token, _parse_period(body.period)
            )
        except GitHubError as exc:
            if exc.status in (401, 403):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="The GitHub token needs Copilot billing admin access "
                    "(org owner or the manage_billing:copilot scope).",
                ) from exc
            if exc.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"No Copilot subscription found for '{body.owner}' (or no access).",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"GitHub error: {exc}"
            ) from exc

    # ---- SSO/SCIM seat sources (Phase 2 build cost) --------------------
    @app.get("/api/build/seat-sources")
    def list_seat_sources(user: CurrentUser) -> list[dict]:
        return seats.list_seat_sources(user["tenant_id"])

    @app.post("/api/build/seat-sources", status_code=status.HTTP_201_CREATED)
    def register_seat_source(body: SeatSourceRequest, user: CurrentUser) -> dict:
        try:
            return seats.register_seat_source(
                user["tenant_id"], body.provider, body.app_id, body.app_label, body.tool, body.plan
            )
        except seats.SeatSourceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/build/seats/sync")
    def sync_idp_seats(body: SeatSyncRequest, user: CurrentUser) -> dict:
        try:
            return seats.sync_idp_seats(user["tenant_id"], _parse_period(body.period))
        except seats.SeatSourceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except (okta.OktaError, entra.EntraError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Identity provider error: {exc}"
            ) from exc

    @app.post("/api/build/claude-code/sync")
    def sync_claude_code_spend(body: SeatSyncRequest, user: CurrentUser) -> dict:
        try:
            return claudecode.import_claude_code_spend(
                user["tenant_id"], _parse_period(body.period)
            )
        except claudecode.ClaudeCodeError as exc:
            code = (
                status.HTTP_400_BAD_REQUEST
                if exc.status in (None, 401, 403)
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.post("/api/build/cursor/sync")
    def sync_cursor_spend(body: SeatSyncRequest, user: CurrentUser) -> dict:
        try:
            return cursorspend.import_cursor_spend(user["tenant_id"], _parse_period(body.period))
        except cursorspend.CursorError as exc:
            code = (
                status.HTTP_400_BAD_REQUEST
                if exc.status in (None, 401, 403)
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.post("/api/build/training")
    def record_training_cost(body: TrainingCostRequest, user: CurrentUser) -> dict:
        return build.record_training_cost(
            user["tenant_id"],
            body.feature_id,
            body.amount,
            body.label,
            _parse_period(body.period),
            body.run_ref,
        )

    # ---- Self-hosted compute pools (open-source inference) --------------
    @app.get("/api/compute/pools")
    def list_compute_pools(user: CurrentUser) -> list[dict]:
        return compute.list_pools(user["tenant_id"])

    @app.post("/api/compute/pools", status_code=status.HTTP_201_CREATED)
    def register_compute_pool(body: ComputePoolRequest, user: CurrentUser) -> dict:
        return compute.register_pool(
            user["tenant_id"], body.name, body.provider_label, body.monthly_cost
        )

    @app.post("/api/compute/allocate")
    def allocate_compute(body: AllocateRequest, user: CurrentUser) -> list[dict]:
        return compute.allocate(user["tenant_id"], _parse_period(body.period), body.pool_id)

    # ---- The three screens (M6) ----------------------------------------

    @app.get("/api/dashboard")
    def get_dashboard(
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        # `period` kept for back-compat (single month); start/end is a custom range.
        s = _parse_period(start) if start else (_parse_period(period) if period else None)
        e = _parse_period(end) if end else None
        return dashboard.dashboard(user["tenant_id"], s, e, range)

    @app.get("/api/features/{feature_id}/detail")
    def feature_detail(
        feature_id: str,
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        # `period` kept for back-compat (single month); range / start+end match the Overview.
        s = _parse_period(start) if start else (_parse_period(period) if period else None)
        e = _parse_period(end) if end else None
        detail = dashboard.feature_detail(user["tenant_id"], feature_id, s, e, range)
        if detail is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found")
        return detail

    @app.get("/api/features/{feature_id}/inference")
    def feature_inference(
        feature_id: str,
        user: CurrentUser,
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        s = _parse_period(start) if start else None
        e = _parse_period(end) if end else None
        return dashboard.feature_inference(user["tenant_id"], feature_id, s, e, range)

    @app.get("/api/features/{feature_id}/opportunities")
    def feature_opportunities(
        feature_id: str,
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        s = _parse_period(start) if start else (_parse_period(period) if period else None)
        e = _parse_period(end) if end else None
        result = optimize_measured.opportunities(user["tenant_id"], feature_id, s, e, range)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found")
        return result

    @app.post("/api/features/{feature_id}/opportunities/apply")
    def apply_opportunity(
        feature_id: str,
        body: ApplyOpportunityRequest,
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        resolved = _parse_period(period) if period else None
        result = optimize_measured.mark_applied(
            user["tenant_id"], feature_id, body.lever, body.projected_monthly, resolved
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found")
        return result

    @app.delete(
        "/api/features/{feature_id}/opportunities/apply",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def unapply_opportunity(
        feature_id: str,
        user: CurrentUser,
        lever: str = Query(pattern=_LEVER_PATTERN),
    ) -> None:
        optimize_measured.unmark_applied(user["tenant_id"], feature_id, lever)

    @app.get("/api/copilot/overview")
    def copilot_overview(
        user: CurrentUser,
        period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        resolved = _parse_period(period) if period else None
        return optimize_measured.copilot_overview(user["tenant_id"], resolved)

    # ---- Internal admin portal (allow-listed admins only) --------------
    @app.get("/api/admin/overview")
    def admin_overview(user: AdminUser) -> dict:
        return admin.overview()

    @app.get("/api/admin/customers")
    def admin_customers(user: AdminUser) -> list[dict]:
        return admin.customers()

    @app.get("/api/admin/customers/{tenant_id}")
    def admin_customer_detail(tenant_id: str, user: AdminUser) -> dict:
        detail = admin.customer_detail(tenant_id)
        if detail is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        return detail

    @app.post("/api/admin/customers/{tenant_id}/connectors")
    def admin_save_connector(tenant_id: str, body: AdminConnectorRequest, user: AdminUser) -> dict:
        try:
            credentials.save_credential(tenant_id, body.connector_type, body.secret, body.label)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/admin/customers/{tenant_id}/connectors/{connector_type}/test")
    def admin_test_connector(tenant_id: str, connector_type: str, user: AdminUser) -> dict:
        return admin.test_connection(tenant_id, connector_type)

    @app.post("/api/admin/customers/{tenant_id}/connectors/{connector_type}/sync")
    def admin_sync_connector(tenant_id: str, connector_type: str, user: AdminUser) -> dict:
        return admin.sync_now(tenant_id, connector_type)

    @app.delete(
        "/api/admin/customers/{tenant_id}/connectors/{connector_type}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def admin_disconnect_connector(tenant_id: str, connector_type: str, user: AdminUser) -> None:
        admin.disconnect(tenant_id, connector_type)

    @app.get("/api/admin/sync-history")
    def admin_sync_history(user: AdminUser) -> list[dict]:
        return admin.sync_history()

    @app.get("/api/admin/errors")
    def admin_errors(user: AdminUser) -> list[dict]:
        return admin.errors()

    @app.post("/api/admin/impersonate/{tenant_id}")
    def admin_impersonate(tenant_id: str, request: Request, user: AdminUser) -> dict:
        company = admin.company_name(tenant_id)
        if company is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        request.session["impersonate_tenant"] = tenant_id
        return {"tenant_id": tenant_id, "company": company}

    @app.delete("/api/admin/impersonate", status_code=status.HTTP_204_NO_CONTENT)
    def admin_stop_impersonate(request: Request, user: RealUser) -> None:
        request.session.pop("impersonate_tenant", None)

    # ---- BYOK: the tenant's own LLM for feature discovery ---------------
    @app.get("/api/settings/discovery-llm")
    def get_discovery_llm(user: CurrentUser) -> dict:
        """The tenant's configuration. Never includes the key — see status()."""
        return discovery_llm.status(user["tenant_id"])

    @app.get("/api/settings/discovery-llm/providers")
    def discovery_llm_providers(user: CurrentUser) -> dict:
        """Known OpenAI-compatible hosts and a suggested base URL for each."""
        return {
            "providers": [
                {"value": p, "base_url": url} for p, url in discovery_llm.PROVIDER_BASE_URLS.items()
            ],
            "default_model": discovery_llm.DEFAULT_DISCOVERY_MODEL,
        }

    @app.put("/api/settings/discovery-llm")
    def save_discovery_llm(body: DiscoveryLlmRequest, user: CurrentUser) -> dict:
        try:
            return discovery_llm.save(
                user["tenant_id"],
                provider=body.provider,
                base_url=body.base_url,
                model=body.model,
                api_key=body.api_key,
                enabled=body.enabled,
                updated_by=user["email"],
            )
        except discovery_llm.ByokError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/settings/discovery-llm/test")
    def test_discovery_llm(body: DiscoveryLlmTestRequest, user: CurrentUser) -> dict:
        """Probe the endpoint. Returns ok/error; the error never carries the key."""
        try:
            return discovery_llm.test_connection(
                user["tenant_id"],
                provider=body.provider,
                base_url=body.base_url,
                model=body.model,
                api_key=body.api_key,
            )
        except discovery_llm.ByokError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.patch("/api/settings/discovery-llm")
    def toggle_discovery_llm(body: DiscoveryLlmEnabledRequest, user: CurrentUser) -> dict:
        """Switch back to Meter's endpoint without discarding the config."""
        return discovery_llm.set_enabled(user["tenant_id"], body.enabled)

    @app.delete("/api/settings/discovery-llm")
    def delete_discovery_llm(user: CurrentUser) -> dict:
        """Delete the configuration and its stored key."""
        return discovery_llm.remove(user["tenant_id"])

    # ---- Provider invoice reconciliation (opt-in, isolated module) -------
    # The whole feature lives in meter/reconciliation and is off unless a
    # tenant turns it on. This line is the only place the rest of the app knows
    # it exists; removing it removes the feature's entire HTTP surface.
    app.include_router(reconciliation_api.build_router(_current_user))

    # ---- Support assistant (knowledge-base grounded) --------------------
    @app.get("/api/assistant/meta")
    def assistant_meta(user: CurrentUser) -> dict:
        """What the widget needs to describe itself honestly.

        `composed` false means no answering model is configured, so replies will
        be handbook excerpts rather than written answers — the UI says so instead
        of pretending.
        """
        return {
            "composed": assistant.env_llm_config() is not None,
            "support_email": SUPPORT_EMAIL,
        }

    @app.post("/api/assistant/chat")
    def assistant_chat(body: AssistantRequest, user: CurrentUser) -> dict:
        try:
            assistant.check_rate(user["tenant_id"])
        except assistant.RateLimited as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
            ) from exc
        return assistant.answer(
            body.question,
            passages=[p.model_dump() for p in body.passages],
            history=[t.model_dump() for t in body.history],
            page=body.page,
        )

    @app.get("/api/dashboard/providers")
    def dashboard_providers(
        user: CurrentUser,
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        s = _parse_period(start) if start else None
        e = _parse_period(end) if end else None
        return dashboard.spend_by_provider(user["tenant_id"], s, e, range)

    @app.get("/api/dashboard/customers")
    def dashboard_customers(
        user: CurrentUser,
        range: Optional[str] = Query(default=None, pattern=_RANGE_RE),
        start: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        end: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    ) -> dict:
        s = _parse_period(start) if start else None
        e = _parse_period(end) if end else None
        return dashboard.spend_by_customer(user["tenant_id"], s, e, range)

    @app.put("/api/features/{feature_id}/usage")
    def set_feature_usage(feature_id: str, body: UsageRequest, user: CurrentUser) -> dict:
        period = _parse_period(body.period) if body.period else None
        try:
            return features.set_usage(
                user["tenant_id"], feature_id, body.active_users, body.events, period
            )
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc

    @app.get("/api/features/categories")
    def feature_categories(user: CurrentUser) -> dict:
        """The category vocabulary + labels, so the UI never hardcodes the list."""
        return {
            "categories": [
                {"value": c, "label": discovery.CATEGORY_LABELS[c]} for c in discovery.CATEGORIES
            ]
        }

    @app.put("/api/features/{feature_id}/category")
    def set_feature_category(feature_id: str, body: CategoryRequest, user: CurrentUser) -> dict:
        """Tag a feature with its product surface (or clear the tag)."""
        try:
            return features.set_category(user["tenant_id"], feature_id, body.category)
        except features.FeatureNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found"
            ) from exc

    # ---- Serve the built web app (production single-service deploy) -----
    # When METER_STATIC_DIR points at the built frontend, the API also serves
    # it: static files where they exist, else index.html (SPA client routing).
    # Registered last so all /api routes take precedence.
    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    static_dir = os.environ.get("METER_STATIC_DIR")
    if not static_dir or not os.path.isdir(static_dir):
        return  # no frontend bundled (e.g. tests, API-only) -> nothing to serve

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    assets = os.path.join(static_dir, "assets")
    if os.path.isdir(assets):
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index_html = os.path.join(static_dir, "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        candidate = os.path.normpath(os.path.join(static_dir, full_path))
        # Stay within the static dir; serve the file if it exists, else the SPA shell.
        if (
            full_path
            and candidate.startswith(os.path.abspath(static_dir))
            and os.path.isfile(candidate)
        ):
            return FileResponse(candidate)
        return FileResponse(index_html)
