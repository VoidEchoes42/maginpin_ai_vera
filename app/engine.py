"""Vera Message Engine — Core decision engine.

Selects the strongest signal from available context and produces
a composed message with CTA, send_as, suppression key, and rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.categories import get_category_policy, is_taboo
from app.state import get_context, get_conversation, is_suppressed, mark_suppressed


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CandidateSignal:
    signal_type: str
    evidence: str
    urgency: int
    actionability: int
    category_fit: int
    merchant_fit: int
    customer_fit: int
    evidence_strength: int
    suppression_key: str
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return (
            self.urgency * 3
            + self.actionability * 3
            + self.category_fit
            + self.merchant_fit
            + self.customer_fit
            + self.evidence_strength
        )


@dataclass
class Decision:
    selected_signal: CandidateSignal
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: List[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_name(merchant: Dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    return identity.get("owner_first_name", "") or identity.get("name", "").split(" ")[0]


def _category_slug(merchant: Dict[str, Any]) -> str:
    return merchant.get("category_slug", "") or ""


def _active_offers(merchant: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [o for o in merchant.get("offers", []) if o.get("status") == "active"]


def _language_pref(merchant: Dict[str, Any]) -> str:
    langs = merchant.get("identity", {}).get("languages", ["en"])
    return langs[0] if langs else "en"


def _customer_language_pref(customer: Optional[Dict[str, Any]]) -> str:
    if not customer:
        return "en"
    return customer.get("identity", {}).get("language_pref", "en")


def _hi_en_mix(lang: str) -> bool:
    return "hi" in lang.lower() or "mix" in lang.lower()


def _status(customer: Optional[Dict[str, Any]]) -> str:
    if not customer:
        return ""
    return customer.get("state", "")


def _consent_scope(customer: Optional[Dict[str, Any]]) -> List[str]:
    if not customer:
        return []
    return customer.get("consent", {}).get("scope", [])


def _slot_label(slot: Dict[str, str]) -> str:
    return slot.get("label", "") or slot.get("iso", "")


def _digest_items(category: Dict[str, Any]) -> List[Dict[str, Any]]:
    return category.get("digest", []) or []


def _peer_ctr(category: Dict[str, Any]) -> float:
    return category.get("peer_stats", {}).get("avg_ctr", 0.030)


def _format_slots(available_slots: List[Dict[str, str]]) -> str:
    if not available_slots:
        return ""
    labels = [_slot_label(s) for s in available_slots[:4]]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} ya {labels[1]}"
    return ", ".join(labels[:-1]) + " ya " + labels[-1]


def _welcome_hi_en(name: str, extra: str = "") -> str:
    parts = []
    if extra:
        parts.append(extra)
    parts.append(f"Hi {name},")
    return " ".join(parts)


def _hindi_suffix(merchant_lang: str, customer_lang: Optional[str] = None) -> str:
    use_hi = _hi_en_mix(merchant_lang) or (customer_lang and _hi_en_mix(customer_lang))
    if not use_hi:
        return ""
    return "Apke liye ready hain. "


# ---------------------------------------------------------------------------
# Signal extractors
# ---------------------------------------------------------------------------

def _extract_signals(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]],
) -> List[CandidateSignal]:
    signals: List[CandidateSignal] = []
    tkind = trigger.get("kind", "")
    tpayload = trigger.get("payload", {})
    tscope = trigger.get("scope", "merchant")

    # --- trigger-scoped signals -------------------------------------------
    if tkind == "research_digest":
        top_id = tpayload.get("top_item_id") or tpayload.get("top_item", {}).get("id")
        top = next((d for d in _digest_items(category) if d.get("id") == top_id), None)
        if top:
            signals.append(CandidateSignal(
                signal_type="research_digest",
                evidence=f"{top.get('title','')} | source: {top.get('source','')}",
                urgency=int(trigger.get("urgency", 2)),
                actionability=8,
                category_fit=10,
                merchant_fit=7,
                customer_fit=0,
                evidence_strength=10,
                suppression_key=trigger.get("suppression_key", f"research:{_category_slug(merchant)}:{tkind}"),
                extra={"digest": top},
            ))

    if tkind == "regulation_change":
        top_id = tpayload.get("top_item_id")
        top = next((d for d in _digest_items(category) if d.get("id") == top_id), None)
        if top:
            signals.append(CandidateSignal(
                signal_type="regulation_change",
                evidence=top.get("title", ""),
                urgency=max(int(trigger.get("urgency", 3)), 4),
                actionability=8,
                category_fit=10,
                merchant_fit=6,
                customer_fit=0,
                evidence_strength=10,
                suppression_key=trigger.get("suppression_key", f"compliance:{_category_slug(merchant)}:2026"),
                extra={"digest": top},
            ))

    if tkind == "perf_spike":
        metric = tpayload.get("metric", "")
        delta = tpayload.get("delta_pct", 0)
        driver = tpayload.get("likely_driver", "")
        if metric and delta:
            signals.append(CandidateSignal(
                signal_type="perf_spike",
                evidence=f"{metric} +{int(delta*100)}% vs baseline {tpayload.get('vs_baseline','')}",
                urgency=int(trigger.get("urgency", 1)),
                actionability=7,
                category_fit=8,
                merchant_fit=9,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"perf_spike:{merchant.get('merchant_id')}:{metric}"),
            ))

    if tkind == "perf_dip":
        metric = tpayload.get("metric", "")
        delta = tpayload.get("delta_pct", 0)
        if metric and delta:
            signals.append(CandidateSignal(
                signal_type="perf_dip",
                evidence=f"{metric} {int(delta*100)}% drop over {tpayload.get('window','7d')}",
                urgency=int(trigger.get("urgency", 3)),
                actionability=7,
                category_fit=7,
                merchant_fit=9,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"perf_dip:{merchant.get('merchant_id')}:{metric}"),
            ))

    if tkind == "seasonal_perf_dip":
        metric = tpayload.get("metric", "")
        delta = tpayload.get("delta_pct", 0)
        season = tpayload.get("season_note", "")
        if metric and delta:
            signals.append(CandidateSignal(
                signal_type="seasonal_perf_dip",
                evidence=f"{metric} {int(delta*100)}% drop — expected seasonal pattern ({season})",
                urgency=int(trigger.get("urgency", 1)),
                actionability=6,
                category_fit=8,
                merchant_fit=8,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"seasonal_dip:{merchant.get('merchant_id')}:2026-Q2"),
            ))

    if tkind == "milestone_reached":
        metric = tpayload.get("metric", "")
        value_now = tpayload.get("value_now", 0)
        milestone = tpayload.get("milestone_value", 0)
        if metric and value_now:
            signals.append(CandidateSignal(
                signal_type="milestone_reached",
                evidence=f"{metric} reached {value_now} (milestone {milestone})",
                urgency=int(trigger.get("urgency", 1)),
                actionability=6,
                category_fit=7,
                merchant_fit=8,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"milestone:{merchant.get('merchant_id')}:{metric}"),
            ))

    if tkind == "dormant_with_vera":
        days = tpayload.get("days_since_last_merchant_message", 0)
        if days:
            signals.append(CandidateSignal(
                signal_type="dormant_with_vera",
                evidence=f"No merchant message for {days} days",
                urgency=int(trigger.get("urgency", 2)),
                actionability=5,
                category_fit=6,
                merchant_fit=7,
                customer_fit=0,
                evidence_strength=7,
                suppression_key=trigger.get("suppression_key", f"dormant:{merchant.get('merchant_id')}:{days}d"),
            ))

    if tkind == "review_theme_emerged":
        theme = tpayload.get("theme", "")
        occurrences = tpayload.get("occurrences_30d", 0)
        if theme and occurrences:
            signals.append(CandidateSignal(
                signal_type="review_theme_emerged",
                evidence=f"{theme} mentioned {occurrences}x in last 30d",
                urgency=int(trigger.get("urgency", 3)),
                actionability=6,
                category_fit=8,
                merchant_fit=7,
                customer_fit=0,
                evidence_strength=7,
                suppression_key=trigger.get("suppression_key", f"review_theme:{merchant.get('merchant_id')}:{theme}"),
            ))

    if tkind == "competitor_opened":
        comp = tpayload.get("competitor_name", "")
        dist = tpayload.get("distance_km", 0)
        their_offer = tpayload.get("their_offer", "")
        if comp:
            signals.append(CandidateSignal(
                signal_type="competitor_opened",
                evidence=f"{comp} opened {dist}km away" + (f" with {their_offer}" if their_offer else ""),
                urgency=int(trigger.get("urgency", 2)),
                actionability=7,
                category_fit=8,
                merchant_fit=8,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"competitor:{merchant.get('merchant_id')}:{comp.lower().replace(' ','')}"),
            ))

    if tkind == "festival_upcoming":
        fest = tpayload.get("festival", "")
        days = tpayload.get("days_until", 0)
        cats = tpayload.get("category_relevance", [])
        if fest and _category_slug(merchant) in cats:
            signals.append(CandidateSignal(
                signal_type="festival_upcoming",
                evidence=f"{fest} in {days} days — category-relevant",
                urgency=int(trigger.get("urgency", 1)),
                actionability=7,
                category_fit=10,
                merchant_fit=6,
                customer_fit=0,
                evidence_strength=7,
                suppression_key=trigger.get("suppression_key", f"festival:{fest.lower()}:{merchant.get('merchant_id')}"),
            ))

    if tkind == "cde_opportunity":
        digest_id = tpayload.get("digest_item_id")
        credits = tpayload.get("credits", 0)
        fee = tpayload.get("fee", "")
        if digest_id:
            top = next((d for d in _digest_items(category) if d.get("id") == digest_id), None)
            title = top.get("title", digest_id) if top else digest_id
            signals.append(CandidateSignal(
                signal_type="cde_opportunity",
                evidence=f"{title} ({credits} credits, {fee})",
                urgency=int(trigger.get("urgency", 1)),
                actionability=7,
                category_fit=9,
                merchant_fit=6,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"cde:{_category_slug(merchant)}:{digest_id}"),
            ))

    if tkind == "curious_ask_due":
        ask_template = tpayload.get("ask_template", "")
        last_ask = tpayload.get("last_ask_at")
        signals.append(CandidateSignal(
            signal_type="curious_ask_due",
            evidence=f"Weekly curious-ask cadence: {ask_template or 'general check-in'}",
            urgency=int(trigger.get("urgency", 1)),
            actionability=5,
            category_fit=8,
            merchant_fit=7,
            customer_fit=0,
            evidence_strength=6,
            suppression_key=trigger.get("suppression_key", f"curious_ask:{merchant.get('merchant_id')}"),
        ))

    if tkind == "winback_eligible":
        days_expired = tpayload.get("days_since_expiry", 0)
        dip = tpayload.get("perf_dip_pct", 0)
        lapsed = tpayload.get("lapsed_customers_added_since_expiry", 0)
        sig = f"subscription expired {days_expired}d ago, perf down {int(dip*100)}%, {lapsed} new lapsed customers"
        signals.append(CandidateSignal(
            signal_type="winback_eligible",
            evidence=sig,
            urgency=int(trigger.get("urgency", 2)),
            actionability=7,
            category_fit=7,
            merchant_fit=9,
            customer_fit=0,
            evidence_strength=8,
            suppression_key=trigger.get("suppression_key", f"winback:{merchant.get('merchant_id')}"),
        ))

    if tkind == "supply_alert":
        molecule = tpayload.get("molecule", "")
        batches = tpayload.get("affected_batches", [])
        mfr = tpayload.get("manufacturer", "")
        if molecule:
            sig = f"Voluntary recall on {molecule} batches {', '.join(batches)} by {mfr}"
            signals.append(CandidateSignal(
                signal_type="supply_alert",
                evidence=sig,
                urgency=5,
                actionability=9,
                category_fit=10,
                merchant_fit=6,
                customer_fit=0,
                evidence_strength=10,
                suppression_key=trigger.get("suppression_key", f"alert:{molecule}:{merchant.get('merchant_id')}"),
            ))

    if tkind == "renewal_due":
        days = tpayload.get("days_remaining", 0)
        amount = tpayload.get("renewal_amount")
        plan = tpayload.get("plan", "")
        sig = f"Subscription {plan} renews in {days} days" + (f", ₹{amount}" if amount else "")
        signals.append(CandidateSignal(
            signal_type="renewal_due",
            evidence=sig,
            urgency=max(int(trigger.get("urgency", 4)), 3),
            actionability=6,
            category_fit=5,
            merchant_fit=9,
            customer_fit=0,
            evidence_strength=8,
            suppression_key=trigger.get("suppression_key", f"renewal:{merchant.get('merchant_id')}:{trigger.get('id','')}"),
        ))

    if tkind == "category_seasonal":
        season = tpayload.get("season", "")
        trends = tpayload.get("trends", [])
        shelf = tpayload.get("shelf_action_recommended", False)
        if trends:
            sig = f"{season}: {', '.join(trends)}" + (" — shelf action recommended" if shelf else "")
            signals.append(CandidateSignal(
                signal_type="category_seasonal",
                evidence=sig,
                urgency=int(trigger.get("urgency", 2)),
                actionability=7,
                category_fit=10,
                merchant_fit=6,
                customer_fit=0,
                evidence_strength=7,
                suppression_key=trigger.get("suppression_key", f"season:{season}:{merchant.get('merchant_id')}"),
            ))

    if tkind == "gbp_unverified":
        estimated = tpayload.get("estimated_uplift_pct", 0)
        path = tpayload.get("verification_path", "")
        if estimated:
            signals.append(CandidateSignal(
                signal_type="gbp_unverified",
                evidence=f"GBP unverified — estimated +{int(estimated*100)}% uplift if verified ({path})",
                urgency=int(trigger.get("urgency", 3)),
                actionability=8,
                category_fit=6,
                merchant_fit=8,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"unverified:{merchant.get('merchant_id')}"),
            ))

    if tkind == "ipl_match_today":
        match = tpayload.get("match", "")
        venue = tpayload.get("venue", "")
        city = tpayload.get("city", "")
        match_time = tpayload.get("match_time_iso", "")
        is_weeknight = tpayload.get("is_weeknight", False)
        if match and city == merchant.get("identity", {}).get("city", ""):
            signals.append(CandidateSignal(
                signal_type="ipl_match_today",
                evidence=f"{match} at {venue} today at {match_time}",
                urgency=int(trigger.get("urgency", 3)),
                actionability=7,
                category_fit=9,
                merchant_fit=7,
                customer_fit=0,
                evidence_strength=8,
                suppression_key=trigger.get("suppression_key", f"ipl:{merchant.get('merchant_id')}:{match.split(' vs ')[0]}"),
            ))

    if tkind == "active_planning_intent":
        topic = tpayload.get("intent_topic", "")
        merchant_msg = tpayload.get("merchant_last_message", "")
        if topic and merchant_msg:
            signals.append(CandidateSignal(
                signal_type="active_planning_intent",
                evidence=f"Merchant asked about {topic}: '{merchant_msg}'",
                urgency=int(trigger.get("urgency", 4)),
                actionability=9,
                category_fit=8,
                merchant_fit=9,
                customer_fit=0,
                evidence_strength=9,
                suppression_key=trigger.get("suppression_key", f"planning:{merchant.get('merchant_id')}:{topic}"),
            ))

    if tkind == "winback_eligible":
        # handled above via generic winback
        pass

    # --- customer-scoped signals ------------------------------------------
    if tscope == "customer" and customer:
        cid = customer.get("customer_id", "")
        cstate = _status(customer)
        consents = _consent_scope(customer)

        if tkind == "recall_due":
            service = tpayload.get("service_due", "checkup")
            due_date = tpayload.get("due_date", "")
            slots = tpayload.get("available_slots", [])
            last_date = customer.get("relationship", {}).get("last_visit", "")
            cname = customer.get("identity", {}).get("name", "")
            slot_str = _format_slots(slots) if slots else ""
            if "recall_reminders" in consents:
                sig = f"{cname}'s {service} recall due since {last_date or due_date}"
                signals.append(CandidateSignal(
                    signal_type="recall_due",
                    evidence=sig,
                    urgency=int(trigger.get("urgency", 3)),
                    actionability=9,
                    category_fit=9,
                    merchant_fit=7,
                    customer_fit=10,
                    evidence_strength=9,
                    suppression_key=trigger.get("suppression_key", f"recall:{cid}:6mo"),
                    extra={"slots": slot_str, "customer_name": cname},
                ))

        if tkind == "customer_lapsed_soft":
            cname = customer.get("identity", {}).get("name", "")
            last = customer.get("relationship", {}).get("last_visit", "")
            sig = f"{cname} soft lapse — last visit {last}"
            if "promotional_offers" in consents:
                signals.append(CandidateSignal(
                    signal_type="customer_lapsed_soft",
                    evidence=sig,
                    urgency=int(trigger.get("urgency", 3)),
                    actionability=6,
                    category_fit=7,
                    merchant_fit=6,
                    customer_fit=9,
                    evidence_strength=7,
                    suppression_key=trigger.get("suppression_key", f"winback:{cid}:soft"),
                    extra={"customer_name": cname},
                ))

        if tkind == "customer_lapsed_hard":
            cname = customer.get("identity", {}).get("name", "")
            days = tpayload.get("days_since_last_visit", 0)
            focus = tpayload.get("previous_focus", "")
            sig = f"{cname} hard lapse — {days} days since last visit"
            if "renewal_reminders" in consents or "winback_offers" in consents:
                signals.append(CandidateSignal(
                    signal_type="customer_lapsed_hard",
                    evidence=sig,
                    urgency=int(trigger.get("urgency", 3)),
                    actionability=7,
                    category_fit=7,
                    merchant_fit=6,
                    customer_fit=9,
                    evidence_strength=8,
                    suppression_key=trigger.get("suppression_key", f"winback:{cid}:hard"),
                    extra={"customer_name": cname, "previous_focus": focus},
                ))

        if tkind == "appointment_tomorrow":
            cname = customer.get("identity", {}).get("name", "")
            if "appointment_reminders" in consents:
                signals.append(CandidateSignal(
                    signal_type="appointment_tomorrow",
                    evidence=f"Appointment reminder for {cname}",
                    urgency=4,
                    actionability=9,
                    category_fit=8,
                    merchant_fit=7,
                    customer_fit=10,
                    evidence_strength=9,
                    suppression_key=trigger.get("suppression_key", f"appt_reminder:{cid}"),
                    extra={"customer_name": cname},
                ))

        if tkind == "chronic_refill_due":
            molecules = tpayload.get("molecule_list", [])
            runs_out = tpayload.get("stock_runs_out_iso", "")
            cname = customer.get("identity", {}).get("name", "")
            if "refill_reminders" in consents:
                signals.append(CandidateSignal(
                    signal_type="chronic_refill_due",
                    evidence=f"{cname}'s {', '.join(molecules)} refills due {runs_out}",
                    urgency=int(trigger.get("urgency", 3)),
                    actionability=9,
                    category_fit=10,
                    merchant_fit=7,
                    customer_fit=10,
                    evidence_strength=9,
                    suppression_key=trigger.get("suppression_key", f"refill:{cid}:{','.join(molecules[:2])}"),
                    extra={"customer_name": cname, "molecules": molecules},
                ))

        if tkind == "trial_followup":
            cname = customer.get("identity", {}).get("name", "")
            trial_date = tpayload.get("trial_date", "")
            options = tpayload.get("next_session_options", [])
            if "program_updates" in consents:
                signals.append(CandidateSignal(
                    signal_type="trial_followup",
                    evidence=f"{cname} trial followup from {trial_date}",
                    urgency=int(trigger.get("urgency", 2)),
                    actionability=8,
                    category_fit=8,
                    merchant_fit=6,
                    customer_fit=9,
                    evidence_strength=7,
                    suppression_key=trigger.get("suppression_key", f"trial_followup:{cid}"),
                    extra={"customer_name": cname, "options": options},
                ))

        if tkind == "wedding_package_followup":
            cname = customer.get("identity", {}).get("name", "")
            wedding = tpayload.get("wedding_date", "")
            window = tpayload.get("next_step_window_open", "")
            if "bridal_package_followup" in consents:
                signals.append(CandidateSignal(
                    signal_type="wedding_package_followup",
                    evidence=f"{cname} — {window} window, wedding {wedding}",
                    urgency=int(trigger.get("urgency", 2)),
                    actionability=8,
                    category_fit=9,
                    merchant_fit=7,
                    customer_fit=9,
                    evidence_strength=8,
                    suppression_key=trigger.get("suppression_key", f"bridal_followup:{cid}"),
                    extra={"customer_name": cname, "wedding_date": wedding},
                ))

    return signals


# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------

def _compose_merchant_message(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    signal: CandidateSignal,
) -> Tuple[str, str, str]:
    """Return (body, cta, rationale)."""
    name = _first_name(merchant)
    lang = _language_pref(merchant)
    hi_en = _hi_en_mix(lang)
    policy = get_category_policy(_category_slug(merchant))
    offers = _active_offers(merchant)
    offer_titles = [o.get("title", "") for o in offers[:3]]

    stype = signal.signal_type
    extra = signal.extra or {}
    evidence = signal.evidence

    # --- Research digest --------------------------------------------------
    if stype == "research_digest":
        digest = extra.get("digest", {})
        title = digest.get("title", evidence)
        source = digest.get("source", "")
        if hi_en:
            body = f"{name}, {title}. Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp you can share?"
        else:
            body = f"{name}, {title}. Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp you can share?"
        if source:
            body += f"  — {source}"
        cta = "open_ended"
        return body, cta, f"External research digest ({source or 'research item'}) with category-relevant clinical anchor"

    # --- Regulation / compliance ------------------------------------------
    if stype == "regulation_change":
        digest = extra.get("digest", {})
        title = digest.get("title", evidence)
        source = digest.get("source", "")
        body = f"{name}, {title}. Action: audit your setup before the effective date + document in your SOPs."
        if source:
            body += f" Source: {source}."
        cta = "binary_yes_no"
        return body, cta, "Compliance alert with concrete action (audit + document SOPs)"

    # --- Performance spike ------------------------------------------------
    if stype == "perf_spike":
        body = f"{name}, {evidence}. Want me to draft a GBP post + a 3-line WhatsApp to ride the momentum?"
        cta = "binary_yes_no"
        return body, cta, "Performance spike — leverage the uptick with immediate content"

    # --- Performance dip --------------------------------------------------
    if stype == "perf_dip":
        body = f"{name}, {evidence}. Before pushing spend, want me to pull the last 7 days of search terms so we can diagnose what changed?"
        cta = "open_ended"
        return body, cta, "Performance dip — diagnostic first, spend second"

    # --- Seasonal dip (expected) ------------------------------------------
    if stype == "seasonal_perf_dip":
        body = f"{name}, {evidence}. Action: skip ad spend now, save it for the post-season window. Want me to draft a retention nudge for your existing members/customers?"
        cta = "binary_yes_no"
        return body, cta, "Seasonal dip reframe — save spend, retain existing base"

    # --- Milestone --------------------------------------------------------
    if stype == "milestone_reached":
        body = f"{name}, {evidence}. Want me to draft a celebration post + a 3-line follow-up offer to push you over the line?"
        cta = "binary_yes_no"
        return body, cta, "Milestone momentum — public celebration + follow-up offer"

    # --- Dormant ----------------------------------------------------------
    if stype == "dormant_with_vera":
        body = f"{name}, it's been a while. Quick check — what's the one thing Vera could help with this week?"
        cta = "open_ended"
        return body, cta, "Low-pressure re-engagement for a dormant merchant"

    # --- Review theme -----------------------------------------------------
    if stype == "review_theme_emerged":
        body = f"{name}, {evidence}. Want me to draft a public response to the recent ones + a short post addressing the concern?"
        cta = "binary_yes_no"
        return body, cta, "Review theme — acknowledge publicly and address"

    # --- Competitor opened ------------------------------------------------
    if stype == "competitor_opened":
        body = f"{name}, {evidence}. Want me to push your {offer_titles[0] if offer_titles else 'best offer'} against their entry this week?"
        cta = "binary_yes_no"
        return body, cta, "Competitor opened nearby — defend with an active offer"

    # --- Festival ---------------------------------------------------------
    if stype == "festival_upcoming":
        fest = extra.get("festival", "") or "the festival"
        body = f"{name}, {fest} is coming. Want me to draft a festive post + a limited-time offer to drive bookings?"
        cta = "binary_yes_no"
        return body, cta, "Festival timing — thematic content + limited offer"

    # --- CDE opportunity --------------------------------------------------
    if stype == "cde_opportunity":
        body = f"{name}, quick heads-up: {evidence}. Want me to add it to your calendar + draft a short note to your patients?"
        cta = "binary_yes_no"
        return body, cta, "CDE opportunity — calendar + patient note"

    # --- Curious ask ------------------------------------------------------
    if stype == "curious_ask_due":
        if hi_en:
            body = f"Hi {name}! Quick check — what's been most in demand this week? I'll turn it into a Google post + a 4-line WhatsApp reply you can reuse."
        else:
            body = f"Hi {name}! Quick check — what's been most in demand this week? I'll turn it into a Google post + a 4-line WhatsApp reply you can reuse."
        cta = "open_ended"
        return body, cta, "Curious-ask cadence — low-pressure engagement"

    # --- Winback ----------------------------------------------------------
    if stype == "winback_eligible":
        body = f"{name}, quick question — is there anything holding you back from re-engaging? Want me to draft a re-entry plan?"
        cta = "open_ended"
        return body, cta, "Winback soft re-engagement after subscription lapse"

    # --- Supply alert -----------------------------------------------------
    if stype == "supply_alert":
        body = f"{name}, {evidence}. Want me to draft a WhatsApp note for affected customers + a replacement-pickup workflow?"
        cta = "binary_yes_no"
        return body, cta, "Supply alert — customer notification + replacement workflow"

    # --- Renewal due ------------------------------------------------------
    if stype == "renewal_due":
        body = f"{name}, {evidence}. Want me to draft a renewal reminder for your customers/members?"
        cta = "binary_yes_no"
        return body, cta, "Subscription renewal due — proactive reminder offer"

    # --- Category seasonal ------------------------------------------------
    if stype == "category_seasonal":
        body = f"{name}, {evidence}. Want me to update your shelf/offer display to match?"
        cta = "binary_yes_no"
        return body, cta, "Category seasonal shift — shelf/offer refresh"

    # --- GBP unverified ---------------------------------------------------
    if stype == "gbp_unverified":
        body = f"{name}, your Google Business Profile is still unverified. Verified listings get ~{int(_peer_ctr(category)*100)}% CTR on average. Want me to walk you through postcard/phone verification in 2 min?"
        cta = "binary_yes_no"
        return body, cta, "Unverified GBP — verification walkthrough offer"

    # --- IPL match --------------------------------------------------------
    if stype == "ipl_match_today":
        body = f"{name}, {evidence}. Push your {offer_titles[0] if offer_titles else 'best offer'} as a match-night special?"
        cta = "binary_yes_no"
        return body, cta, "IPL match day — match-night special offer"

    # --- Active planning intent -------------------------------------------
    if stype == "active_planning_intent":
        topic = extra.get("intent_topic", "")
        body = f"{name}, here's a starter version — you can edit:\n\n{topic.replace('_',' ').title()} package:\n- Tier 1: 10 units @ competitive price\n- Tier 2: 25+ units @ bulk discount\n\nWant me to draft the full proposal + a 3-line outreach to the target list?"
        cta = "binary_yes_no"
        return body, cta, f"Merchant explicitly planning {topic} — draft + outreach"

    # --- Customer-scoped --------------------------------------------------
    if tscope == "customer" and customer:
        cname = extra.get("customer_name", customer.get("identity", {}).get("name", ""))
        c_lang = _customer_language_pref(customer)
        hi_c = _hi_en_mix(c_lang)

        if stype == "recall_due":
            service = extra.get("service_due", "checkup")
            slots = extra.get("slots", "")
            offer_str = offer_titles[0] if offer_titles else "your usual service"
            slot_part = f" Available: {slots}." if slots else ""
            if hi_c:
                body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} yahan. Your {service} recall is due.{slot_part} {offer_str}. Reply 1 to confirm, or tell us a time that works."
            else:
                body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} here. Your {service} recall is due.{slot_part} {offer_str}. Reply 1 to confirm, or tell us a time that works."
            cta = "multi_choice_slot"
            return body, cta, f"Recall reminder for {cname} with slot offer"

        if stype == "customer_lapsed_soft":
            offer_str = offer_titles[0] if offer_titles else "a check-in offer"
            if hi_c:
                body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} yahan. It's been a while — {offer_str} is ready for you. Want to book?"
            else:
                body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} here. It's been a while — {offer_str} is ready for you. Want to book?"
            cta = "binary_yes_no"
            return body, cta, f"Soft winback for {cname}"

        if stype == "customer_lapsed_hard":
            focus = extra.get("previous_focus", "")
            if hi_c:
                body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} yahan. We miss you — no pressure, just wanted to check in. Reply YES if you'd like a fresh start."
            else:
                body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} here. We miss you — no pressure, just wanted to check in. Reply YES if you'd like a fresh start."
            cta = "binary_yes_no"
            return body, cta, f"Hard lapse winback for {cname}"

        if stype == "appointment_tomorrow":
            if hi_c:
                body = f"Hi {cname}, reminder: appointment tomorrow with {merchant.get('identity', {}).get('name','')}. Reply YES to confirm, or call to reschedule."
            else:
                body = f"Hi {cname}, reminder: appointment tomorrow with {merchant.get('identity', {}).get('name','')}. Reply YES to confirm, or call to reschedule."
            cta = "binary_confirm_cancel"
            return body, cta, f"Appointment reminder for {cname}"

        if stype == "chronic_refill_due":
            mols = extra.get("molecules", [])
            mol_str = ", ".join(mols) if mols else "your medicines"
            body = (
                f"Namaste — {merchant.get('identity', {}).get('name','')} yahan. "
                f"{cname}'s {mol_str} refill is due. Same dose, same brand ready. "
                f"Free home delivery by tomorrow. Reply CONFIRM to dispatch."
            )
            cta = "binary_confirm_cancel"
            return body, cta, f"Chronic refill reminder for {cname}"

        if stype == "trial_followup":
            options = extra.get("options", [])
            opt = options[0].get("label", "") if options else "next session"
            body = f"Hi {cname}, great to have you in! Want to book {opt} for your next session?"
            cta = "binary_yes_no"
            return body, cta, f"Trial followup for {cname}"

        if stype == "wedding_package_followup":
            wedding = extra.get("wedding_date", "")
            body = f"Hi {cname}, {merchant.get('identity', {}).get('name','')} here. With your wedding on {wedding}, now is the perfect time to start skin prep. Want me to block your preferred slot?"
            cta = "binary_yes_no"
            return body, cta, f"Bridal followup for {cname}"

    # --- Fallback merchant -------------------------------------------------
    offer_str = offer_titles[0] if offer_titles else ""
    if offer_str:
        body = f"{name}, you have an active offer: {offer_str}. Want me to help you get more eyes on it this week?"
        cta = "open_ended"
    else:
        body = f"{name}, quick question — what's one thing Vera can help with this week?"
        cta = "open_ended"
    return body, cta, "Fallback — merchant-facing general nudge"


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------

def _classify_intent(message: str, conversation_id: str) -> str:
    lower = message.lower().strip()
    # Auto-reply detection
    auto_phrases = [
        "thank you for contacting",
        "our team will respond shortly",
        "automated assistant",
        "auto-reply",
        "auto reply",
    ]
    for phrase in auto_phrases:
        if phrase in lower:
            return "auto_reply"

    # Hard no / opt-out
    hard_no = ["stop", "not interested", "don't message", "do not send", "unsubscribe", "remove me"]
    for phrase in hard_no:
        if phrase in lower:
            return "hard_no"

    # Explicit intent handoff
    intent_phrases = ["ok let's do it", "lets do it", "go ahead", "yes,", "yes ", "please send", "proceed", "confirm"]
    for phrase in intent_phrases:
        if phrase in lower:
            return "intent_handoff"

    # Request for details
    question_words = ["how", "what", "when", "where", "why", "?", "can you", "could you"]
    for w in question_words:
        if w in lower:
            return "question"

    # Off-topic / hostile
    hostile = ["useless", "spam", "bothering", "gst", "file my", "help me with my"]
    for phrase in hostile:
        if phrase in lower:
            return "off_topic"

    # Positive
    positive = ["yes", "ok", "okay", "sounds good", "great", "send", "done", "interested"]
    for phrase in positive:
        if lower.startswith(phrase):
            return "positive"

    return "neutral"


def _reply_action(
    conversation_id: str,
    merchant_id: str,
    message: str,
    turn_number: int,
    merchant: Dict[str, Any],
    customer: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    intent = _classify_intent(message, conversation_id)

    if intent == "auto_reply":
        return {
            "action": "wait",
            "wait_seconds": 14400 if turn_number >= 3 else 1800,
            "body": "",
            "cta": "none",
            "rationale": "Detected auto-reply pattern; backing off to wait for real merchant response."
            if turn_number >= 3
            else "Possible auto-reply; backing off briefly.",
        }

    if intent == "hard_no":
        return {
            "action": "end",
            "body": "",
            "cta": "none",
            "rationale": "Merchant explicitly opted out. Closing conversation gracefully.",
        }

    if intent == "off_topic":
        return {
            "action": "send",
            "body": "I'll leave that topic aside — it's outside what I can help with directly. Want to continue where we left off?",
            "cta": "open_ended",
            "rationale": "Off-topic request politely deflected; conversation redirected.",
        }

    if intent == "intent_handoff":
        # Advance to action
        category = getattr(_reply_action, "_last_category", {})
        last_trigger = getattr(_reply_action, "_last_trigger", {})
        sig = CandidateSignal(
            signal_type="active_planning_intent",
            evidence="Merchant confirmed intent",
            urgency=4, actionability=9, category_fit=8, merchant_fit=9, customer_fit=0, evidence_strength=9,
            suppression_key="planning:action"
        )
        body, cta, rationale = _compose_merchant_message(category, merchant, sig)
        return {
            "action": "send",
            "body": body,
            "cta": cta,
            "rationale": f"Intent handoff — switching to action. {rationale}",
        }

    if intent == "positive":
        body, cta, rationale = _compose_merchant_message(
            getattr(_reply_action, "_last_category", {}),
            merchant,
            CandidateSignal(
                signal_type="active_planning_intent",
                evidence="Merchant accepted",
                urgency=4, actionability=9, category_fit=8, merchant_fit=9, customer_fit=0, evidence_strength=9,
                suppression_key="planning:action"
            ),
        )
        return {
            "action": "send",
            "body": body,
            "cta": cta,
            "rationale": f"Merchant accepted; advancing to action. {rationale}",
        }

    if intent == "question":
        # Try to answer from context — be brief
        offer_str = ""
        offers = _active_offers(merchant)
        if offers:
            offer_str = f"Your active offer: {offers[0].get('title','')}. "
        body = f"{offer_str}Want me to pull the exact details you need?"
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_no",
            "rationale": "Merchant asked for details — brief answer + next-step CTA.",
        }

    # neutral
    return {
        "action": "send",
        "body": f"Got it. Want me to continue with what we were discussing, or is there something specific you need?",
        "cta": "open_ended",
        "rationale": "Neutral response — keep conversation moving.",
    }


# ---------------------------------------------------------------------------
# Main compose entrypoint
# ---------------------------------------------------------------------------

def compose(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    turn_number: Optional[int] = None,
    merchant_reply: Optional[str] = None,
) -> Dict[str, Any]:
    """Core composition function.

    If merchant_reply is provided, handle reply; otherwise compose fresh.
    """
    tscope = trigger.get("scope", "merchant")

    # --- Reply path -------------------------------------------------------
    if merchant_reply and conversation_id:
        # cache category/trigger for reply reuse
        _reply_action._last_category = category  # type: ignore[attr-defined]
        _reply_action._last_trigger = trigger      # type: ignore[attr-defined]
        result = _reply_action(conversation_id, merchant.get("merchant_id", ""), merchant_reply, turn_number or 1, merchant, customer)
        return result

    # --- Fresh compose path -----------------------------------------------
    candidate_signals = _extract_signals(category, merchant, trigger, customer)

    # If no signals extracted, suppress
    if not candidate_signals:
        return {
            "action": "none",
            "body": "",
            "cta": "none",
            "send_as": "vera",
            "suppression_key": trigger.get("suppression_key", "none"),
            "rationale": "No actionable signal from current context; suppressing this tick.",
            "template_name": "none",
            "template_params": [],
        }

    # Apply suppression
    for sig in candidate_signals:
        if is_suppressed(sig.suppression_key):
            sig.actionability = max(1, sig.actionability - 5)
            sig.evidence_strength = max(1, sig.evidence_strength - 3)

    # Pick highest-scoring signal (deterministic: sort by score DESC, then lexicographic signal type)
    candidate_signals.sort(key=lambda s: (-s.score, s.signal_type))
    best = candidate_signals[0]

    # Check for duplicate body in conversation
    if conversation_id:
        hist = get_conversation(conversation_id)
        # Not checking duplicate here; suppression handles that

    body, cta, rationale = _compose_merchant_message(category, merchant, best)

    # Validate: no taboos
    policy = get_category_policy(_category_slug(merchant))
    for taboo in policy.get("taboos", []):
        if taboo.lower() in body.lower():
            # Replace with safe alternative
            body = body.replace(taboo, "strong")
            body = re.sub(r'\s+', ' ', body).strip()

    # Trim body if absurdly long
    if len(body) > 1500:
        body = body[:1497] + "..."

    # Ensure single CTA
    if cta == "none" and best.customer_fit > 0 and _status(customer) in ("lapsed_soft", "lapsed_hard"):
        cta = "binary_yes_no"

    send_as = "merchant_on_behalf" if (tscope == "customer" and customer) else "vera"

    # Mark suppression
    mark_suppressed(best.suppression_key)

    return {
        "action": "send",
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": best.suppression_key,
        "rationale": rationale,
        "template_name": f"vera_{best.signal_type}_v1",
        "template_params": [name := _first_name(merchant), best.evidence[:80]],
    }
