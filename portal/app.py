"""Client portal API (FastAPI).

Isolation rule: the client is decided ONLY by the signed-in user's Microsoft
Entra tenant (the 'tid' claim in a verified token). No endpoint accepts a client
id from the URL or the body, so a user can only ever reach their own company's
database, Claude key and Power BI workspace.

Run (development):  DATIA_ENV=dev uvicorn portal.app:app --reload
"""
from __future__ import annotations

import re
import secrets
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, insert, select

from backbone import settings
from backbone.agents.assistant import answer
from backbone.context import ClientContext, open_client
from backbone.db import assistant_messages, audit, documents, pipeline_runs, review_queue, staging_invoices

WEB = Path(__file__).parent / "web" / "portal.html"
MSAL_SCRIPT = "https://cdn.jsdelivr.net/npm/@azure/msal-browser@3.27.0/lib/msal-browser.min.js"
MSAL_SRI = "sha384-8zg7Eb13Jv4vByCkXozj1LKSUykPghS+Zp1Q9BA8lYkMDbushxoDjh+RQ+4r1DNd"   # pinned file hash


def portal_csp(nonce: str) -> str:
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "connect-src 'self' https://login.microsoftonline.com; frame-src https://app.powerbi.com; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def render_portal(env: str, nonce: str) -> str:
    """Wraps the portal page (written without its own html/head/body) in a full document;
    inline scripts get the per-request nonce allowed by the Content-Security-Policy."""
    page = WEB.read_text(encoding="utf-8")
    m = re.search(r"<title>.*?</title>", page, re.S)
    title = m.group(0) if m else "<title>Portal Datia</title>"
    page = page.replace(title, "", 1).replace("<script>", f'<script nonce="{nonce}">')
    msal = "" if env == "dev" else f'<script src="{MSAL_SCRIPT}" integrity="{MSAL_SRI}" crossorigin="anonymous"></script>'
    return ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
            f"{title}{msal}<style>[hidden]{{display:none!important}}body{{margin:0}}</style></head><body>{page}</body></html>")


def _utc(row: dict) -> dict:
    """The database stores UTC without a zone; send ISO strings ending in Z so browsers show local time correctly."""
    return {k: (v.isoformat(timespec="seconds") + "Z" if isinstance(v, datetime) else v) for k, v in row.items()}
from backbone.pipeline import approve_review_item, ingest_document, reject_review_item
from backbone.tenancy import ClientRegistry, UnknownClient


REVIEWER_ROLE = "Reviewer"                  # Entra app role allowed to approve/reject invoices
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
_jwks_clients: dict = {}                    # one cached key client per tenant


@dataclass(frozen=True)
class Identity:
    client_id: str
    user_id: str
    upn: str | None = None
    roles: frozenset = field(default_factory=frozenset)


class ApproveBody(BaseModel):
    corrections: dict = {}


class RejectBody(BaseModel):
    note: str = ""


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


MAX_QUESTIONS_PER_USER_PER_DAY = 100


def _verify_entra_token(token: str) -> dict:
    """Validates a Microsoft Entra ID access token issued for our portal API.
    Accepts v1 and v2 issuers; DATIA_API_AUDIENCE may list the App ID URI and the client id, comma-separated.
    Guest (B2B) accounts are refused: only members of the client's own tenant get in."""
    import jwt
    audiences = [a.strip() for a in settings.API_AUDIENCE.split(",") if a.strip()]
    if not audiences:
        raise HTTPException(500, "DATIA_API_AUDIENCE is not configured")
    unverified = jwt.decode(token, options={"verify_signature": False})
    tid = unverified.get("tid")
    if not tid:
        raise HTTPException(401, "token without tenant")
    if not _known_tenant(tid):               # only registered companies' keys are ever fetched or cached
        raise HTTPException(403, "your company is not registered")
    if tid not in _jwks_clients:
        _jwks_clients[tid] = jwt.PyJWKClient(f"https://login.microsoftonline.com/{tid}/discovery/v2.0/keys")
    key = _jwks_clients[tid].get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=audiences,
                        options={"require": ["exp", "tid", "oid", "iss"], "verify_iss": False})
    if claims["iss"] not in (f"https://login.microsoftonline.com/{tid}/v2.0", f"https://sts.windows.net/{tid}/"):
        raise HTTPException(401, "wrong issuer")
    idp = claims.get("idp")
    if idp and idp not in (claims["iss"], f"https://sts.windows.net/{tid}/"):
        raise HTTPException(403, "guest accounts are not allowed")
    return claims


_known_tenant: Callable[[str], bool] = lambda tid: False


def create_app(registry: ClientRegistry, context_factory: Callable[[ClientRegistry, str], ClientContext] = open_client,
               env: str | None = None) -> FastAPI:
    global _known_tenant
    env = env or settings.ENV

    def known(tid: str) -> bool:
        try:
            registry.by_entra_tenant(tid)
            return True
        except UnknownClient:
            return False
    _known_tenant = known
    app = FastAPI(title="Datia portal API", version="0.1.0")
    contexts: dict[str, ClientContext] = {}

    def identity(request: Request) -> Identity:
        dev_client = request.headers.get("X-Dev-Client")
        if dev_client is not None:
            if env != "dev":
                raise HTTPException(401, "development login disabled")
            try:
                registry.get(dev_client)
            except UnknownClient:
                raise HTTPException(403, "unknown client")
            roles = request.headers.get("X-Dev-Roles", REVIEWER_ROLE)
            return Identity(dev_client, request.headers.get("X-Dev-User", "dev"),
                            request.headers.get("X-Dev-Upn"), frozenset(r for r in roles.split(",") if r))
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "sign in required")
        try:
            claims = _verify_entra_token(auth[7:])
            client = registry.by_entra_tenant(claims["tid"])
        except UnknownClient:
            raise HTTPException(403, "your company is not registered")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "invalid token")
        return Identity(client.client_id, claims["oid"], claims.get("upn") or claims.get("preferred_username"),
                        frozenset(claims.get("roles", [])))

    def ctx_for(who: Identity = Depends(identity)) -> tuple[Identity, ClientContext]:
        if who.client_id not in contexts:
            contexts[who.client_id] = context_factory(registry, who.client_id)
        return who, contexts[who.client_id]

    def require_reviewer(who: Identity):
        if REVIEWER_ROLE not in who.roles:
            raise HTTPException(403, "only reviewers can approve or reject invoices")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def portal_page():
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(render_portal(env, nonce), headers={"Content-Security-Policy": portal_csp(nonce),
                                                                "X-Content-Type-Options": "nosniff",
                                                                "Referrer-Policy": "same-origin"})

    @app.get("/auth-config")
    def auth_config():
        if env == "dev":
            return {"mode": "dev"}
        return {"mode": "entra", "clientId": settings.PORTAL_CLIENT_ID,
                "authority": "https://login.microsoftonline.com/organizations", "scope": settings.PORTAL_API_SCOPE}

    @app.get("/config")
    def config(dep=Depends(ctx_for)):
        who, ctx = dep
        p = ctx.config.powerbi
        embed = (f"https://app.powerbi.com/reportEmbed?reportId={p.report_id}&autoAuth=true&ctid={p.tenant_id}"
                 if p and p.report_id else None)
        return {"company": ctx.config.name, "user": who.upn or who.user_id, "roles": sorted(who.roles),
                "report_embed_url": embed}

    @app.get("/documents")
    def list_documents(dep=Depends(ctx_for)):
        _, ctx = dep
        with ctx.engine.connect() as conn:
            rows = conn.execute(select(documents.c.filename, documents.c.channel, documents.c.status,
                                       documents.c.received_at).order_by(desc(documents.c.id)).limit(50)).mappings()
            return [_utc(dict(r)) for r in rows]

    @app.get("/me")
    def me(dep=Depends(ctx_for)):
        who, ctx = dep
        return {"company": ctx.config.name, "user": who.user_id}

    @app.get("/review-queue")
    def list_review(dep=Depends(ctx_for)):
        _, ctx = dep
        with ctx.engine.connect() as conn:
            q = (select(review_queue.c.id, review_queue.c.reason, review_queue.c.created_at, staging_invoices.c.ncf,
                        staging_invoices.c.issuer_rnc, staging_invoices.c.issuer_name, staging_invoices.c.buyer_rnc,
                        staging_invoices.c.issue_date, staging_invoices.c.subtotal, staging_invoices.c.itbis,
                        staging_invoices.c.total, staging_invoices.c.currency, staging_invoices.c.source_ref)
                 .join(staging_invoices, staging_invoices.c.id == review_queue.c.staging_id)
                 .where(review_queue.c.status == "open").order_by(review_queue.c.id))
            return [_utc(dict(r)) for r in conn.execute(q).mappings()]

    @app.post("/review-queue/{item_id}/approve")
    def approve(item_id: int, body: ApproveBody, dep=Depends(ctx_for)):
        who, ctx = dep
        require_reviewer(who)
        try:
            return approve_review_item(ctx, item_id, who.user_id, body.corrections)
        except LookupError:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, f"invalid correction: {e}")

    @app.post("/review-queue/{item_id}/reject")
    def reject(item_id: int, body: RejectBody, dep=Depends(ctx_for)):
        who, ctx = dep
        require_reviewer(who)
        try:
            reject_review_item(ctx, item_id, who.user_id, body.note)
        except LookupError:
            raise HTTPException(404, "not found")
        return {"rejected": True}

    @app.post("/documents")
    async def upload(file: UploadFile = File(...), dep=Depends(ctx_for)):
        who, ctx = dep
        safe_name = Path(file.filename or "upload").name or "upload"     # never trust a path from the browser
        suffix = Path(safe_name).suffix.lower()
        if suffix not in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".xml"):
            raise HTTPException(415, "PDF, image or e-CF XML only")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large (15 MB max)")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / safe_name
            path.write_bytes(data)
            doc_id = ingest_document(ctx, path, "upload")
        return {"document_id": doc_id, "duplicate": doc_id is None}

    @app.post("/ask")
    def ask(body: AskBody, dep=Depends(ctx_for)):
        who, ctx = dep
        if ctx.powerbi is None:
            raise HTTPException(409, "Power BI not connected yet")
        if ctx.config.rls_enabled and not who.upn:
            raise HTTPException(403, "your account has no user name for row-level security")
        with ctx.engine.connect() as conn:
            # the limit resets at Dominican midnight; created_at is stored in UTC
            local_midnight = datetime.now(settings.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            since = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
            asked_today = conn.execute(select(func.count()).select_from(assistant_messages).where(
                assistant_messages.c.user_id == who.user_id, assistant_messages.c.role == "user",
                assistant_messages.c.created_at >= since)).scalar() or 0
        if asked_today >= MAX_QUESTIONS_PER_USER_PER_DAY:
            raise HTTPException(429, "daily question limit reached; try again tomorrow")
        with ctx.engine.connect() as conn:
            rows = conn.execute(select(assistant_messages.c.role, assistant_messages.c.content)
                                .where(assistant_messages.c.user_id == who.user_id)
                                .order_by(desc(assistant_messages.c.id)).limit(10)).all()
        history = [{"role": r.role, "content": r.content} for r in reversed(rows)]
        text, queries = answer(ctx.claude, ctx.model_agent, ctx.powerbi, ctx.config.name, ctx.config.model_description,
                               body.question, history, impersonated_user=who.upn)
        with ctx.engine.begin() as conn:
            conn.execute(insert(assistant_messages), [
                {"user_id": who.user_id, "role": "user", "content": body.question},
                {"user_id": who.user_id, "role": "assistant", "content": text}])
            audit(conn, f"user:{who.user_id}", "assistant_question", {"queries": queries})
        return {"answer": text}

    @app.get("/runs/latest")
    def latest_run(dep=Depends(ctx_for)):
        _, ctx = dep
        with ctx.engine.connect() as conn:
            r = conn.execute(select(pipeline_runs).order_by(desc(pipeline_runs.c.id)).limit(1)).mappings().first()
        return _utc(dict(r)) if r else {}

    return app


def _default_app() -> FastAPI:
    try:
        return create_app(ClientRegistry.from_file(settings.CLIENTS_FILE))
    except FileNotFoundError:
        return create_app(ClientRegistry({}))


app = _default_app()
