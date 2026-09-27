"""Quick sanity test for the Vera engine endpoints."""

from __future__ import annotations

import requests

BASE = "http://localhost:8080"


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")


def test_healthz():
    r = requests.get(f"{BASE}/v1/healthz", timeout=5)
    check("healthz 200", r.status_code == 200)
    data = r.json()
    check("healthz status ok", data.get("status") == "ok")
    check("healthz has contexts_loaded", "contexts_loaded" in data)


def test_metadata():
    r = requests.get(f"{BASE}/v1/metadata", timeout=5)
    check("metadata 200", r.status_code == 200)
    data = r.json()
    check("metadata has team_name", "team_name" in data)


def test_context_push():
    payload = {
        "scope": "category",
        "context_id": "dentists",
        "version": 1,
        "delivered_at": "2026-04-26T09:45:00Z",
        "payload": {"slug": "dentists", "voice": {"tone": "peer_clinical"}},
    }
    r = requests.post(f"{BASE}/v1/context", json=payload, timeout=5)
    check("context push 200", r.status_code == 200)
    data = r.json()
    check("context accepted", data.get("accepted") is True)

    # idempotent re-push same version -> 409
    r2 = requests.post(f"{BASE}/v1/context", json=payload, timeout=5)
    check("context stale_version 409", r2.status_code == 409)

    # higher version -> 200
    payload["version"] = 2
    payload["payload"]["voice"]["tone"] = "peer_clinical_v2"
    r3 = requests.post(f"{BASE}/v1/context", json=payload, timeout=5)
    check("context version bump 200", r3.status_code == 200)


def test_tick_and_reply():
    # push merchant + trigger
    requests.post(f"{BASE}/v1/context", json={
        "scope": "merchant", "context_id": "m_test", "version": 1,
        "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {
            "merchant_id": "m_test", "category_slug": "dentists",
            "identity": {"name": "Test Dental", "city": "Delhi", "locality": "CP", "languages": ["en"], "owner_first_name": "Test"},
            "subscription": {"status": "active", "plan": "Pro", "days_remaining": 30},
            "performance": {"window_days": 30, "views": 1000, "calls": 10, "directions": 20, "ctr": 0.020, "leads": 3, "delta_7d": {"views_pct": 0.1, "calls_pct": 0.05, "ctr_pct": 0.02}},
            "offers": [{"id": "o1", "title": "Checkup @ ₹299", "status": "active"}],
            "conversation_history": [], "customer_aggregate": {"total_unique_ytd": 100, "lapsed_180d_plus": 20, "retention_6mo_pct": 0.40}, "signals": []
        }
    }, timeout=5)

    requests.post(f"{BASE}/v1/context", json={
        "scope": "trigger", "context_id": "trg_test", "version": 1,
        "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {
            "id": "trg_test", "scope": "merchant", "kind": "research_digest", "source": "external",
            "merchant_id": "m_test", "customer_id": None,
            "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
            "urgency": 2, "suppression_key": "research:dentists:test", "expires_at": "2026-05-03T00:00:00Z"
        }
    }, timeout=5)

    requests.post(f"{BASE}/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1,
        "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {
            "slug": "dentists",
            "voice": {"tone": "peer_clinical", "taboos": ["guaranteed", "100% safe"], "salutation_examples": ["Dr. {first_name}"], "code_mix": "hindi_english_natural"},
            "offer_catalog": [{"id": "den_001", "title": "Dental Cleaning @ ₹299"}],
            "peer_stats": {"avg_ctr": 0.030},
            "digest": [{"id": "d_2026W17_jida_fluoride", "kind": "research", "title": "3-month fluoride recall outperforms 6-month for high-risk adult caries", "source": "JIDA Oct 2026, p.14"}],
            "patient_content_library": [], "seasonal_beats": [], "trend_signals": []
        }
    }, timeout=5)

    r = requests.post(f"{BASE}/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_test"]}, timeout=10)
    check("tick 200", r.status_code == 200)
    data = r.json()
    check("tick returns actions", "actions" in data)
    actions = data.get("actions", [])
    check("tick has >=1 action", len(actions) >= 1)

    if actions:
        a = actions[0]
        check("action has required fields", all(k in a for k in ("conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta", "suppression_key", "rationale")))

        # reply test
        conv = a["conversation_id"]
        r2 = requests.post(f"{BASE}/v1/reply", json={
            "conversation_id": conv, "merchant_id": "m_test", "customer_id": None,
            "from_role": "merchant", "message": "Ok let's do it. What's next?", "received_at": "2026-04-26T10:42:00Z", "turn_number": 2
        }, timeout=10)
        check("reply 200", r2.status_code == 200)
        rdata = r2.json()
        check("reply action is send", rdata.get("action") == "send")
        check("reply has body", bool(rdata.get("body")))


if __name__ == "__main__":
    print("Sanity tests for Vera engine")
    try:
        test_healthz()
        test_metadata()
        test_context_push()
        test_tick_and_reply()
    except requests.exceptions.ConnectionError:
        print("FAIL: Could not connect to bot. Start it with: uvicorn app.main:app --host 0.0.0.0 --port 8080")
