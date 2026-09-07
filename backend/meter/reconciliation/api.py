"""HTTP surface for reconciliation. One router, mounted under /api/reconciliation.

Two gates apply to every route below. `CurrentUser` is the existing session
dependency, so a request without a session never arrives. `_enabled` then
refuses unless this tenant has switched the module on — so a tenant that has
not enabled it gets 404 from every route, whether or not the UI would have
shown it. The switch is not a UI convenience.

Every query runs inside that user's tenant transaction, so row-level security
answers the isolation question rather than a WHERE clause.
"""

# NOTE: no `from __future__ import annotations` here, deliberately. The routes
# below are typed with an Annotated alias built inside build_router, and
# postponed annotations would leave FastAPI a string it cannot resolve from
# module globals — it would silently treat the dependency as a query parameter.
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from . import engine, flag, imports, report
from .flag import ReconciliationError
from .imports import MAX_BYTES

#: Providers a statement can be imported for. Deliberately the ones whose spend
#: Meter already tracks — importing a bill with nothing to compare it to
#: would produce a page of "incomplete data" and no insight.
PROVIDERS = (
    "anthropic",
    "openai",
    "google",
    "bedrock",
    "azure",
    "openrouter",
    "together",
    "fireworks",
    "groq",
    "mistral",
    "xai",
    "perplexity",
    "cohere",
    "replicate",
    "vercel",
    "litellm",
    "portkey",
    "helicone",
    "modal",
    "elevenlabs",
)


class SettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    tolerance_abs: Optional[Decimal] = Field(default=None, ge=0)
    tolerance_pct: Optional[Decimal] = Field(default=None, ge=0, le=100)


class PreviewRequest(BaseModel):
    content: str = Field(max_length=MAX_BYTES)
    mapping: Optional[dict] = None


class ImportRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    filename: str = Field(default="import.csv", max_length=300)
    content: str = Field(max_length=MAX_BYTES)
    mapping: Optional[dict] = None
    provider_account: Optional[str] = Field(default=None, max_length=200)
    replace_import_id: Optional[str] = Field(default=None, max_length=64)


class RunRequest(BaseModel):
    import_id: str = Field(min_length=1, max_length=64)


def build_router(current_user) -> APIRouter:
    """The reconciliation router. `current_user` is the app's existing session
    dependency, passed in so this module never reaches back into api.py."""
    router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])
    User = Annotated[dict, Depends(current_user)]

    def _enabled(user: dict) -> str:
        """Tenant id, if this tenant has the module on. 404 otherwise — a
        disabled module should not even admit its routes exist."""
        tenant_id = user["tenant_id"]
        if not flag.is_enabled(tenant_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Reconciliation is not enabled."
            )
        return tenant_id

    def _bad(exc: ReconciliationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # -- settings: readable whether or not it is on, so the UI can offer it ---
    @router.get("/settings")
    def get_settings(user: User) -> dict:
        return {**flag.settings(user["tenant_id"]), "providers": list(PROVIDERS)}

    @router.put("/settings")
    def put_settings(body: SettingsRequest, user: User) -> dict:
        try:
            return flag.update(
                user["tenant_id"],
                enabled=body.enabled,
                tolerance_abs=body.tolerance_abs,
                tolerance_pct=body.tolerance_pct,
                actor=user.get("email"),
            )
        except ReconciliationError as exc:
            raise _bad(exc) from exc

    # -- import ---------------------------------------------------------------
    @router.post("/preview")
    def preview(body: PreviewRequest, user: User) -> dict:
        _enabled(user)
        try:
            return imports.preview(body.content, body.mapping)
        except ReconciliationError as exc:
            raise _bad(exc) from exc

    @router.post("/imports", status_code=status.HTTP_201_CREATED)
    def create_import(body: ImportRequest, user: User) -> dict:
        tenant_id = _enabled(user)
        if body.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Unsupported provider: {body.provider}")
        try:
            return imports.commit(
                tenant_id,
                provider=body.provider,
                filename=body.filename,
                content=body.content,
                mapping=body.mapping,
                provider_account=body.provider_account,
                actor=user.get("email"),
                replace_import_id=body.replace_import_id,
            )
        except ReconciliationError as exc:
            raise _bad(exc) from exc

    @router.get("/imports")
    def list_imports(user: User) -> list[dict]:
        return imports.history(_enabled(user))

    @router.delete("/imports/{import_id}")
    def delete_import(import_id: str, user: User) -> dict:
        tenant_id = _enabled(user)
        try:
            return imports.remove(tenant_id, import_id, actor=user.get("email"))
        except ReconciliationError as exc:
            raise _bad(exc) from exc

    # -- runs -----------------------------------------------------------------
    @router.post("/runs", status_code=status.HTTP_201_CREATED)
    def create_run(body: RunRequest, user: User) -> dict:
        tenant_id = _enabled(user)
        try:
            return engine.calculate(tenant_id, import_id=body.import_id, actor=user.get("email"))
        except ReconciliationError as exc:
            raise _bad(exc) from exc

    @router.get("/runs")
    def list_runs(user: User) -> list[dict]:
        return engine.runs(_enabled(user))

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, user: User) -> dict:
        tenant_id = _enabled(user)
        try:
            return engine.run_detail(tenant_id, run_id)
        except ReconciliationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/report.csv")
    def export_report(run_id: str, user: User) -> Response:
        tenant_id = _enabled(user)
        try:
            filename, body = report.export_csv(tenant_id, run_id)
        except ReconciliationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        from .common import record_audit, tenant_conn

        with tenant_conn(tenant_id) as conn:
            record_audit(conn, tenant_id, "report_exported", actor=user.get("email"), run_id=run_id)
        return Response(
            content=body,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router
