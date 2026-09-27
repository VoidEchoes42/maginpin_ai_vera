"""Vera Message Engine — FastAPI application.

Implements the exact API contract specified in challenge-testing-brief.md.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app.engine import compose
from app.state import (
    get_context,
    get_context_counts,
    get_conversation,
    get_context_version,
    is_suppressed,
    mark_suppressed,
    store_context,
    store_conversation,
    uptime_seconds,
)

app = FastAPI(title="Vera Message Engine", version="1.0.0")
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("vera")
START = time.time()

# ---------------------------------------------------------------------------
# Metadata configuration
# ---------------------------------------------------------------------------
METADATA = {
    "team_name": "Vera Engine",
    "team_members": ["Vera AI"],
    "model": "deterministic_rule_engine_v1",
    "approach": (
        "Deterministic decision engine scoring candidate signals across "
        "urgency, actionability, category fit, merchant fit, and evidence strength. "
        "Selects the single strongest signal per tick and composes a template-filled "
        "message grounded in actual context data only."
    ),
    "contact_email": "vera-engine@example.com",
    "version": "1.0.0",
    "submitted_at": "2026-04-26T08:00:00Z",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str = Field(..., description="One of: category, merchant, customer, trigger")
    context_id: str
    version: int = Field(..., ge=1)
    payload: Dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: List[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = Field(..., description="merchant | customer")
    message: str
    received_at: str
    turn_number: int = Field(..., ge=1)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = get_context_counts()
    for scope in ("category", "merchant", "customer", "trigger"):
        counts.setdefault(scope, 0)
    return {
        "status": "ok",
        "uptime_seconds": uptime_seconds(),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return METADATA


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of category/merchant/customer/trigger, got: {body.scope}"},
        )

    existing_version = get_context_version(body.scope, body.context_id)
    if existing_version is not None and existing_version >= body.version:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": existing_version},
        )

    result = store_context(body.scope, body.context_id, body.version, body.payload)
    if result.get("stale"):
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": result["current_version"]},
        )

    return {
        "accepted": True,
        "ack_id": f"ack_{body.scope}_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    now_str = body.now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    log.debug("tick now=%s available=%s", now_str, body.available_triggers)

    for tid in body.available_triggers:
        trigger = get_context("trigger", tid)
        log.debug("tick trigger %s -> %s", tid, bool(trigger))
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        log.debug("tick merchant_id=%s customer_id=%s", merchant_id, customer_id)
        if not merchant_id:
            continue

        merchant = get_context("merchant", merchant_id)
        log.debug("tick merchant=%s", bool(merchant))
        if not merchant:
            continue

        category_slug = merchant.get("category_slug", "")
        category = get_context("category", category_slug)
        log.debug("tick category_slug=%s category=%s", category_slug, bool(category))
        if not category:
            # fallback: no category context, skip
            continue

        customer = get_context("customer", customer_id) if customer_id else None

        # Suppression check
        sk = trigger.get("suppression_key", "")
        sup = is_suppressed(sk)
        log.debug("tick suppression_key=%s suppressed=%s", sk, sup)
        if sk and sup:
            continue

        # Compose
        try:
            result = compose(
                category=category,
                merchant=merchant,
                trigger=trigger,
                customer=customer,
            )
        except Exception:
            log.exception("tick compose failed for trigger %s", tid)
            continue
        log.debug("tick compose result action=%s", result.get("action"))

        action = result.get("action", "none")
        if action == "none":
            continue

        # Build conversation id
        if customer_id:
            conv_id = f"conv_{customer_id}_{tid}"
        else:
            conv_id = f"conv_{merchant_id}_{tid}"

        # Record conversation state
        store_conversation(conv_id, {
            "ts": now_str,
            "from": "vera",
            "body": result.get("body", ""),
            "trigger_id": tid,
            "suppression_key": result.get("suppression_key", ""),
        })

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result.get("send_as", "vera"),
            "trigger_id": tid,
            "template_name": result.get("template_name", f"vera_{trigger.get('kind','generic')}_v1"),
            "template_params": result.get("template_params", []),
            "body": result.get("body", ""),
            "cta": result.get("cta", "open_ended"),
            "suppression_key": result.get("suppression_key", sk),
            "rationale": result.get("rationale", ""),
        })

        if len(actions) >= 20:
            break

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    merchant_id = body.merchant_id or ""
    customer_id = body.customer_id

    merchant = get_context("merchant", merchant_id) if merchant_id else {}
    category_slug = merchant.get("category_slug", "")
    category = get_context("category", category_slug) if category_slug else {}

    trigger: Dict[str, Any] = {"scope": "merchant", "kind": "active_planning_intent", "suppression_key": ""}
    if customer_id:
        customer = get_context("customer", customer_id)
    else:
        customer = None

    # Load prior conversation
    history = get_conversation(body.conversation_id)
    store_conversation(body.conversation_id, {
        "ts": body.received_at,
        "from": body.from_role,
        "body": body.message,
        "turn_number": body.turn_number,
    })

    result = compose(
        category=category,
        merchant=merchant,
        trigger=trigger,
        customer=customer,
        conversation_id=body.conversation_id,
        turn_number=body.turn_number,
        merchant_reply=body.message,
    )

    action = result.get("action", "send")

    if action == "end":
        return {"action": "end", "rationale": result.get("rationale", "")}

    if action == "wait":
        return {"action": "wait", "wait_seconds": int(result.get("wait_seconds", 1800)), "rationale": result.get("rationale", "")}

    body_text = result.get("body", "")
    return {
        "action": "send",
        "body": body_text,
        "cta": result.get("cta", "open_ended"),
        "rationale": result.get("rationale", ""),
    }
