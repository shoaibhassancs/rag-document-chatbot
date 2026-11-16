"""Check that the Groq and Voyage keys work, with one tiny call each.

Prints only "OK" or "FAIL (reason)". Never prints a key. Run it sparingly: the Voyage call
uses one of the 3 requests per minute available without a payment method.

    uv run python -m scripts.check_keys
"""

import sys

import httpx

from app.config import Settings, get_settings


def check_groq(client: httpx.Client, settings: Settings) -> str:
    """List models (uses no tokens) and confirm the configured models are available."""
    key = settings.groq_api_key
    if key is None:
        return "FAIL (GROQ_API_KEY not set)"
    base_url = settings.answer_llm_base_url
    try:
        resp = client.get(
            f"{base_url}/models", headers={"Authorization": f"Bearer {key.get_secret_value()}"}
        )
    except httpx.HTTPError as exc:
        return f"FAIL ({type(exc).__name__})"
    if resp.status_code != 200:
        return f"FAIL (HTTP {resp.status_code})"
    available = {m.get("id") for m in resp.json().get("data", [])}
    wanted = {
        settings.answer_llm_model,
        settings.answer_fallback_model,
        settings.judge_llm_model,
        settings.rewrite_llm_model,
    }
    missing = sorted(wanted - available)
    if missing:
        return f"FAIL (models not listed: {', '.join(missing)})"
    return "OK"


def check_voyage(client: httpx.Client, settings: Settings) -> str:
    """Embed one word and confirm the vector has the configured dimension."""
    key = settings.voyage_api_key
    if key is None:
        return "FAIL (VOYAGE_API_KEY not set)"
    try:
        resp = client.post(
            f"{settings.embedding_base_url}/embeddings",
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            json={
                "input": ["ping"],
                "model": settings.embedding_model,
                "input_type": "query",
                "output_dimension": settings.embedding_dimensions,
            },
        )
    except httpx.HTTPError as exc:
        return f"FAIL ({type(exc).__name__})"
    if resp.status_code == 429:
        return "FAIL (rate limited: 3 requests/minute; wait a minute and retry)"
    if resp.status_code != 200:
        return f"FAIL (HTTP {resp.status_code})"
    vector = resp.json()["data"][0]["embedding"]
    if len(vector) != settings.embedding_dimensions:
        return f"FAIL (got {len(vector)} dimensions, config says {settings.embedding_dimensions})"
    return "OK"


def main() -> int:
    settings = get_settings()
    with httpx.Client(timeout=20) as client:
        results = {
            "groq": check_groq(client, settings),
            "voyage": check_voyage(client, settings),
        }
    for name, result in results.items():
        print(f"{name}: {result}")
    return 0 if all(r == "OK" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
