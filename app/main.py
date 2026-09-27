"""Vera Message Engine — Flask application.

Implements the exact API contract specified in challenge-testing-brief.md.
Uses Flask instead of FastAPI to avoid pydantic version conflicts on Render.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

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

app = Flask(__name__)
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
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    counts = get_context_counts()
    for scope in ("category", "merchant", "customer", "trigger"):
        counts.setdefault(scope, 0)
    return jsonify({
        "status": "ok",
        "uptime_seconds": uptime_seconds(),
        "contexts_loaded": counts,
    })


@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify(METADATA)


@app.route("/v1/context", methods=["POST"])
def push_context():
    body = request.get_json(force=True)
    if not body:
        return jsonify({"accepted": False, "reason": "invalid_json", "details": "empty body"}), 400

    scope = body.get("scope")
    if scope not in ("category", "merchant", "customer", "trigger"):
        return jsonify({
            "accepted": False, "reason": "invalid_scope",
            "details": f"scope must be one of category/merchant/customer/trigger, got: {scope}"
        }), 400

    context_id = body.get("context_id")
    version = body.get("version")
    payload = body.get("payload")

    if not context_id or version is None or payload is None:
        return jsonify({"accepted": False, "reason": "missing_fields", "details": "context_id, version, and payload are required"}), 400

    existing_version = get_context_version(scope, context_id)
    if existing_version is not None and existing_version >= version:
        return jsonify({"accepted": False, "reason": "stale_version", "current_version": existing_version}), 409

    result = store_context(scope, context_id, version, payload)
    if result.get("stale"):
        return jsonify({"accepted": False, "reason": "stale_version", "current_version": result["current_version"]}), 409

    return jsonify({
        "accepted": True,
        "ack_id": f"ack_{scope}_{context_id}_v{version}",
        "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


@app.route("/v1/tick", methods=["POST"])
def tick():
    body = request.get_json(force=True) or {}
    now_str = body.get("now", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    available_triggers = body.get("available_triggers", [])
    log.debug("tick now=%s available=%s", now_str, available_triggers)

    actions = []
    for tid in available_triggers:
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
            continue

        customer = get_context("customer", customer_id) if customer_id else None

        sk = trigger.get("suppression_key", "")
        sup = is_suppressed(sk)
        log.debug("tick suppression_key=%s suppressed=%s", sk, sup)
        if sk and sup:
            continue

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

        if customer_id:
            conv_id = f"conv_{customer_id}_{tid}"
        else:
            conv_id = f"conv_{merchant_id}_{tid}"

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

    return jsonify({"actions": actions})


@app.route("/v1/reply", methods=["POST"])
def reply():
    body = request.get_json(force=True) or {}
    conversation_id = body.get("conversation_id", "")
    merchant_id = body.get("merchant_id", "")
    customer_id = body.get("customer_id")
    from_role = body.get("from_role", "merchant")
    message = body.get("message", "")
    received_at = body.get("received_at", datetime.now(timezone.utc).isoformat())
    turn_number = body.get("turn_number", 1)

    merchant = get_context("merchant", merchant_id) if merchant_id else {}
    category_slug = merchant.get("category_slug", "")
    category = get_context("category", category_slug) if category_slug else {}

    trigger: Dict[str, Any] = {"scope": "merchant", "kind": "active_planning_intent", "suppression_key": ""}
    customer = get_context("customer", customer_id) if customer_id else None

    history = get_conversation(conversation_id)
    store_conversation(conversation_id, {
        "ts": received_at,
        "from": from_role,
        "body": message,
        "turn_number": turn_number,
    })

    result = compose(
        category=category,
        merchant=merchant,
        trigger=trigger,
        customer=customer,
        conversation_id=conversation_id,
        turn_number=turn_number,
        merchant_reply=message,
    )

    action = result.get("action", "send")

    if action == "end":
        return jsonify({"action": "end", "rationale": result.get("rationale", "")})

    if action == "wait":
        return jsonify({
            "action": "wait",
            "wait_seconds": int(result.get("wait_seconds", 1800)),
            "rationale": result.get("rationale", ""),
        })

    return jsonify({
        "action": "send",
        "body": result.get("body", ""),
        "cta": result.get("cta", "open_ended"),
        "rationale": result.get("rationale", ""),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    app.run(host="0.0.0.0", port=port, debug=False)
