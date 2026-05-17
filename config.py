from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Token is optional at startup — each user provides their own via the UI.
    # The env var, if set, acts as a fallback (useful for local development).
    gitbook_api_token: str = ""
    gitbook_base_url: str = "https://api.gitbook.com/v1"
    anthropic_api_key: str = ""  # optional — enables AI-powered Ask

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


ORG_ID = "0mbgBjArWXoMupyWdFkH"

PORTAL_CONFIG = {
    "brand": {
        "label": "Brand",
        "color": "#e8364f",
        "version": "v13.0",
        "description": "For advertisers running affiliate & partner programs",
        "sections": {
            "guides": {
                "label": "Guides",
                "space_id": "wMLlMoFBtKJa8ptd3zaw",
                "space_title": "I'm a Brand (EN)",
            },
            "reference": {
                "label": "API Reference",
                "space_id": "VRLPOlXbBZtmNg1zw3te",
                "space_title": "Brand API Reference v13",
            },
        },
    },
    "partner": {
        "label": "Partner",
        "color": "#3b8ef3",
        "version": "v15.0",
        "description": "For publishers & affiliates who earn commissions",
        "sections": {
            "guides": {
                "label": "Guides",
                "space_id": "b2rE79d9UhOKZQLgzSqx",
                "space_title": "I'm a Partner (EN)",
            },
            "reference": {
                "label": "API Reference",
                "space_id": "IC4JA8OntdV7pbqJoNkI",
                "space_title": "Partner API Reference v15",
            },
        },
    },
    "agency": {
        "label": "Agency",
        "color": "#9b6ef5",
        "version": "v8.0",
        "description": "For agencies managing multiple brand accounts",
        "sections": {
            "guides": {
                "label": "Guides",
                "space_id": "okTNfAIjBtFchXJ9pC2q",
                "space_title": "I'm an Agency (EN)",
            },
            "reference": {
                "label": "API Reference",
                "space_id": "3an35NjrMgGKemtN6H10",
                "space_title": "Agency API Reference v3",
            },
        },
    },
    "hub": {
        "label": "Integrations Hub",
        "color": "#3cc48c",
        "version": None,
        "description": "REST APIs, SDKs, recipes, and developer tools",
        "sections": {
            "guides": {
                "label": "All Content",
                "space_id": "iVbdCghMC6mw51rrphG6",
                "space_title": "Integrations Hub",
            },
        },
    },
}

KEY_CONCEPTS = [
    {
        "term": "UTT",
        "definition": "Universal Tracking Tag — the JavaScript snippet brands install on their site to track conversions.",
    },
    {
        "term": "irclickid",
        "definition": "Query parameter appended by impact.com to referral URLs for click attribution back to a partner.",
    },
    {
        "term": "Action / Conversion",
        "definition": "A tracked event (sale, lead, app install) that is attributed to a partner and eligible for payout.",
    },
    {
        "term": "Contract",
        "definition": "The commission rules that govern the payout relationship between a brand and a specific partner.",
    },
    {
        "term": "Program",
        "definition": "The affiliate program container — previously called a Campaign — that partners apply to join.",
    },
    {
        "term": "MMP",
        "definition": "Mobile Measurement Partner — third-party attribution platforms such as Adjust, AppsFlyer, and Branch.",
    },
    {
        "term": "CLO",
        "definition": "Card-Linked Offer — a card-based tracking method currently in Beta on the impact.com platform.",
    },
    {
        "term": "Advocate",
        "definition": "impact.com's referral and loyalty product (formerly SaaSquatch) for running refer-a-friend programs.",
    },
]
