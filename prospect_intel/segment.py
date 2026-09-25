"""Buyer-segment profiles: what counts as a relevant signal depends on who
is being sold to. A media/OTT CRM (Zee5-class) cares about ad budgets,
content slates, subscriptions and partnerships — not refinery throughput.

Active profile via PROSPECT_SEGMENT (default: media_ott, this deployment).
Replace default_collateral with real items (inputs/SYSTEM_INPUTS.md §1) —
they are honest placeholders, labeled as such everywhere they appear.
"""
from __future__ import annotations
import os

MEDIA_OTT = {
    "key": "media_ott",
    "label": "Media / OTT pre-sales (Zee5-class CRM agent)",
    "topics": ("advertising ad-spend brand partnership content slate "
               "streaming OTT subscription viewership programming merger "
               "sponsorship"),
    "trade_outlets": ("exchange4media", "afaqs", "ETBrandEquity",
                      "campaignindia", "medianews4u", "tvnews4u",
                      "financialexpress-brandwagon"),
    "rss_angles": ("{co} strategy expansion earnings",
                   "{co} hiring jobs careers",
                   "{co} advertising brand partnership",
                   "{co} OTT streaming content subscription"),
    "default_collateral": (
        "[DEFAULT - replace with real collateral] OTT ad inventory: "
        "targeted pre-roll and mid-roll slots against premium regional + "
        "Hindi content audiences.",
        "[DEFAULT - replace with real collateral] Brand partnership playbook: "
        "co-branded content tentpoles and sponsorship packages around "
        "originals and live events.",
        "[DEFAULT - replace with real collateral] Subscriber insight package: "
        "audience segments by genre affinity for media-planning pitches.",
    ),
    "opening_style": ("For a media buyer: ask about their content or "
                      "advertising calendar first — '{top}' connects to it."),
}

GENERIC = {
    "key": "generic",
    "label": "Generic B2B pre-sales",
    "topics": ("strategy priorities business earnings hiring expansion "
               "investment"),
    "trade_outlets": (),
    "rss_angles": ("{co} strategy expansion earnings",
                   "{co} hiring jobs careers"),
    "default_collateral": ("[DEFAULT - replace with real collateral] platform "
                           "scaling case study",),
    "opening_style": "",
}

_PROFILES = {"media_ott": MEDIA_OTT, "generic": GENERIC}


def active() -> dict:
    key = os.environ.get("PROSPECT_SEGMENT", "media_ott")
    if key not in _PROFILES:
        try:
            from . import audit
            audit.log("segment_unknown",
                      {"requested": key, "fallback": "media_ott"})
        except Exception:
            pass
        key = "media_ott"
    return _PROFILES[key]


def relevance_query() -> str:
    seg = active()
    return ("company strategy priorities business earnings hiring expansion "
            "investment " + seg["topics"])


def outlet_boost(url: str, title: str = "") -> float:
    blob = (url + " " + title).lower()
    return 0.15 if any(t in blob for t in active()["trade_outlets"]) else 0.0


def default_collateral() -> list[str]:
    return list(active()["default_collateral"])
