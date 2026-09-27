"""Vera Message Engine — Category policies and voice rules."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Category voice rules loaded from dataset + overrides
CATEGORY_VOICE: Dict[str, Dict[str, Any]] = {
    "dentists": {
        "tone": "peer_clinical",
        "salutation": ["Dr. {first_name}", "Doc"],
        "code_mix": "hindi_english_natural",
        "taboos": ["guaranteed", "100% safe", "completely cure", "miracle", "best in city", "doctor approved"],
        "allowed_vocab": [
            "fluoride varnish", "scaling", "caries", "occlusion", "bruxism",
            "endodontic", "periodontal", "implant", "aligner", "veneer",
            "OPG", "IOPA", "RCT", "CAD/CAM", "zirconia", "PFM"
        ],
        "tone_examples": [
            "Worth a look — JIDA Oct 2026 p.14",
            "This one likely affects your high-risk adult cohort",
        ],
        "suitable_actions": [
            "research_review", "compliance_audit", "recall_campaign",
            "patient_education_post", "offer_refresh", "milestone_acknowledgment"
        ],
        "suitable_ctas": ["open_ended", "binary_yes_no", "multi_choice_slot"],
        "useful_metrics": ["ctr", "calls", "high_risk_adult_count", "lapsed_180d_plus", "retention_6mo_pct"],
    },
    "salons": {
        "tone": "warm_practical",
        "salutation": ["{first_name}"],
        "code_mix": "hindi_english_natural",
        "taboos": ["guaranteed", "100% safe", "miracle", "best in city"],
        "allowed_vocab": ["balayage", "keratin", "bridal", "facial", "manicure", "pedicure", "highlights"],
        "tone_examples": ["Your bridal bookings are trending up in Kapra — want to capture more?"],
        "suitable_actions": [
            "bridal_package_push", "seasonal_look_post", "stylist_spotlight",
            "offer_refresh", "lapsed_winback", "dormant_reengage"
        ],
        "suitable_ctas": ["open_ended", "binary_yes_no"],
        "useful_metrics": ["ctr", "calls", "repeat_customer_pct", "lapsed_90d_plus"],
    },
    "restaurants": {
        "tone": "operator_to_operator",
        "salutation": ["{first_name}"],
        "code_mix": "hindi_english_natural",
        "taboos": ["guaranteed", "100% safe", "miracle", "best in city"],
        "allowed_vocab": ["thali", "covers", "dine-in", "delivery", "Swiggy", "Zomato", "BOGO", "combo"],
        "tone_examples": ["Saturday IPL shifts covers — push your BOGO as delivery-only today"],
        "suitable_actions": [
            "match_night_special", "delivery_optimization", "corporate_bulk_offer",
            "festival_menu_push", "review_response_draft"
        ],
        "suitable_ctas": ["open_ended", "binary_yes_no"],
        "useful_metrics": ["ctr", "calls", "dine_in_orders_30d", "delivery_orders_30d"],
    },
    "gyms": {
        "tone": "coach_to_operator",
        "salutation": ["{first_name}"],
        "code_mix": "hindi_english_natural",
        "taboos": ["guaranteed", "100% safe", "miracle", "best in city"],
        "allowed_vocab": ["membership", "trial", "PT session", "HIIT", "yoga", "attendance", "churn"],
        "tone_examples": ["Your 245 members are your baseline — keep them through the April dip"],
        "suitable_actions": [
            "membership_renewal", "trial_campaign", "attendance_challenge",
            "class_launch", "lapsed_member_winback", "seasonal_reframe"
        ],
        "suitable_ctas": ["open_ended", "binary_yes_no"],
        "useful_metrics": ["ctr", "calls", "total_active_members", "monthly_churn_pct", "trial_to_paid_pct"],
    },
    "pharmacies": {
        "tone": "trustworthy_precise",
        "salutation": ["{first_name}"],
        "code_mix": "hindi_english_natural",
        "taboos": ["guaranteed", "100% safe", "completely cure", "miracle", "best in city"],
        "allowed_vocab": ["refill", "chronic", "ORS", "home delivery", "senior citizen", "OTC"],
        "tone_examples": [
            "Urgent: voluntary recall on atorvastatin batches AT2024-1102, AT2024-1108 — "
            "22 chronic-Rx customers affected. Want me to draft their WhatsApp note?"
        ],
        "suitable_actions": [
            "supply_alert", "chronic_refill_reminder", "seasonal_stock_plan",
            "delivery_optimization", "senior_citizen_offer"
        ],
        "suitable_ctas": ["open_ended", "binary_yes_no", "binary_confirm_cancel"],
        "useful_metrics": ["ctr", "calls", "repeat_customer_pct", "chronic_rx_count"],
    },
}


def get_category_policy(category_slug: str) -> Dict[str, Any]:
    """Return category policy, defaulting to generic if unknown."""
    return CATEGORY_VOICE.get(category_slug, {
        "tone": "friendly_practical",
        "salutation": ["{first_name}"],
        "code_mix": "hindi_english_natural",
        "taboos": ["guaranteed", "100% safe", "miracle", "best in city"],
        "suitable_actions": ["general_nudge"],
        "suitable_ctas": ["open_ended", "binary_yes_no"],
        "useful_metrics": ["ctr", "calls", "views"],
    })


def is_taboo(category_slug: str, phrase: str) -> bool:
    """Check if a phrase contains a taboo word for this category."""
    policy = get_category_policy(category_slug)
    lower = phrase.lower()
    for taboo in policy.get("taboos", []):
        if taboo.lower() in lower:
            return True
    return False
