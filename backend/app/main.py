"""FastAPI app: four worker-facing modules plus the Bangla UI.

Every route returns a friendly Bangla message on failure. No uploaded file is ever
written to disk - ``analyse_contract`` works on the bytes in memory and the request
handler drops them as soon as it returns.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .contracts.pipeline import analyse_contract
from .errors import FraudShieldError, RateLimited, log_event
from .licence.search import data_summary, lookup as licence_lookup
from .rag.answer import answer_question
from .rag.index import corpus_size
from .rules.engine import check_fee, fee_caps
from .safety.guard import (
    CONSENT_BN,
    DEMO_DATA_BANNER_BN,
    DISCLAIMER_BN,
    PASSPORT_BN,
    UNSAFE_VERDICT_NOTE_BN,
    redact_pii,
)
from .schemas import (
    AskRequest,
    ErrorResponse,
    FeeCheckRequest,
    MetaResponse,
)

app = FastAPI(
    title="Migrant Worker Fraud Shield API",
    version="0.1.0",
    description=(
        "Prototype. Reports what the dated BMET list contains and what the rules "
        "compute. Never states that an agency or contract is safe. Not legal advice."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Rate limiting: in-process sliding windows. Enough for one small instance; a
# multi-instance deployment needs a shared store (documented in the README).
# --------------------------------------------------------------------------- #
_HITS: dict[str, deque[float]] = defaultdict(deque)
_PER_MINUTE = 30


def _rate_limit(request: Request, key: str, limit: int, window: float) -> None:
    now = time.monotonic()
    bucket = _HITS[f"{key}:{request.client.host if request.client else 'unknown'}"]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        log_event("rate_limited", key=key)
        raise RateLimited(key)
    bucket.append(now)


@app.exception_handler(FraudShieldError)
async def handle_domain_error(_: Request, exc: FraudShieldError) -> JSONResponse:
    status = 429 if isinstance(exc, RateLimited) else 422
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(error=exc.__class__.__name__, message_bn=exc.message_bn).model_dump(),
    )


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
def _meta() -> MetaResponse:
    data_date, is_official, rows = data_summary()
    caps = fee_caps().get("caps", {})
    return MetaResponse(
        mode="llm" if settings.llm_enabled else "offline",
        agency_data_date=data_date,
        agency_data_is_official=is_official,
        fee_cap_dates={code: cap.get("effective_date") for code, cap in caps.items()},
        helpline_bn=settings.helpline_bn,
        helpline_verified=settings.helpline_verified,
        partner_name_bn=settings.partner_name_bn,
        partner_logo_url=settings.partner_logo_url,
        act_source="Overseas Employment and Migrants Act 2013 (official English text)",
        disclaimer_bn=DISCLAIMER_BN,
        voice_hint_bn="প্রশ্নটি মুখে বলতে পারেন — নিচের মাইকের বোতাম চাপুন।",
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": "llm" if settings.llm_enabled else "offline",
        "act_sections_indexed": corpus_size(),
        "agency_rows": data_summary()[2],
    }


@app.get("/meta", response_model=MetaResponse)
def meta() -> MetaResponse:
    return _meta()


@app.get("/api/meta", response_model=MetaResponse)
def api_meta() -> MetaResponse:
    return _meta()


@app.get("/api/texts")
def texts() -> dict:
    """All fixed Bangla copy in one place, so the UI never invents wording."""
    return {
        "consent_bn": CONSENT_BN,
        "disclaimer_bn": DISCLAIMER_BN,
        "demo_banner_bn": DEMO_DATA_BANNER_BN,
        "never_safe_note_bn": UNSAFE_VERDICT_NOTE_BN,
        "passport_note_bn": PASSPORT_BN,
        "helpline_bn": settings.helpline_bn,
    }


# --------------------------------------------------------------------------- #
# Module 1: licence checker
# --------------------------------------------------------------------------- #
@app.post("/api/licence")
async def api_licence(request: Request, payload: dict) -> dict:
    _rate_limit(request, "licence", _PER_MINUTE, 60.0)
    query = str(payload.get("query") or payload.get("rl") or payload.get("name") or "")
    return licence_lookup(query).model_dump()


# --------------------------------------------------------------------------- #
# Module 2 + 3: contract explainer + red-flag detector
# --------------------------------------------------------------------------- #
@app.post("/api/contract")
async def api_contract(
    request: Request,
    file: UploadFile = File(...),
    answers: str = Form("{}"),
) -> dict:
    _rate_limit(request, "contract", settings.max_uploads_per_day, 86_400.0)
    import json

    try:
        parsed_answers = json.loads(answers or "{}")
        if not isinstance(parsed_answers, dict):
            parsed_answers = {}
    except json.JSONDecodeError:
        parsed_answers = {}

    raw = await file.read()
    started = time.monotonic()
    analysis = analyse_contract(
        filename=file.filename or "upload",
        data=raw,
        declared_type=file.content_type,
        answers=parsed_answers,
    )
    del raw  # bytes are never persisted; analysis worked on the in-memory copy
    log_event("contract_request_done", seconds=round(time.monotonic() - started, 2))
    return analysis.model_dump()


# --------------------------------------------------------------------------- #
# Module 3b: standalone fee check
# --------------------------------------------------------------------------- #
@app.post("/api/fee-check")
async def api_fee_check(request: Request, payload: FeeCheckRequest) -> dict:
    _rate_limit(request, "fee", _PER_MINUTE, 60.0)
    if payload.quoted_fee_bdt < 0:
        raise HTTPException(status_code=422, detail="negative_fee")
    return check_fee(payload.destination, payload.quoted_fee_bdt).model_dump()


# --------------------------------------------------------------------------- #
# Module 4: complaint / rights Q&A
# --------------------------------------------------------------------------- #
@app.post("/api/ask")
async def api_ask(request: Request, payload: AskRequest) -> dict:
    _rate_limit(request, "ask", _PER_MINUTE, 60.0)
    question = redact_pii(payload.question).text
    return answer_question(question).model_dump()


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    import traceback

    log_event(
        "unhandled_error",
        error=type(exc).__name__,
        detail=redact_pii(str(exc))[:300],
        where=(traceback.extract_tb(exc.__traceback__)[-1].name if exc.__traceback__ else ""),
    )
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="internal_error",
            message_bn="কিছু একটা সমস্যা হয়েছে। আবার চেষ্টা করুন।",
        ).model_dump(),
    )


# --------------------------------------------------------------------------- #
# Static frontend (same origin, so no CORS pain in the pilot)
# --------------------------------------------------------------------------- #
from pathlib import Path  # noqa: E402

_FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend"
if _FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
