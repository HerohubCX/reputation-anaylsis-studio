"""
pipeline/analyzer.py - Analyse Google reviews with Claude AI.
"""

import json
import logging
import re

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert automotive dealership reputation analyst.
Analyse the provided customer reviews and return a detailed JSON report.
Be objective, thorough, and base all conclusions strictly on the review data provided."""

ANALYSIS_PROMPT_TEMPLATE = """Analyse the following customer reviews for {dealer_name} and return a JSON object with exactly this structure (no extra keys, no markdown fences):

{{
  "dealer_name": "{dealer_name}",
  "total_reviews": <integer>,
  "average_rating": <float, 1 decimal place>,
  "rating_distribution": {{
    "5": <count>, "4": <count>, "3": <count>, "2": <count>, "1": <count>
  }},
  "summary": "<2-3 sentence executive summary>",
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "weaknesses": ["<weakness 1>", "<weakness 2>"],
  "top_themes": [
    {{"theme": "<theme name>", "sentiment": "positive|negative|mixed", "count": <integer>}},
    ...
  ],
  "staff_mentions": [
    {{"name": "<first name>", "mentions": <integer>, "sentiment": "positive|negative|mixed"}},
    ...
  ],
  "recommendation_rate": <float 0-100, percentage of reviewers who recommend>,
  "response_rate": <float 0-100, percentage of reviews that received a dealer response>,
  "recent_trend": "improving|stable|declining",
  "key_improvements": ["<improvement suggestion 1>", "<improvement suggestion 2>"],
  "sample_positive_reviews": [
    {{"author": "<name>", "rating": <int>, "text": "<review text>", "date": "<YYYY-MM-DD>"}}
  ],
  "sample_negative_reviews": [
    {{"author": "<name>", "rating": <int>, "text": "<review text>", "date": "<YYYY-MM-DD>"}}
  ]
}}

REVIEWS CSV:
{reviews_csv}

Return ONLY the JSON object, no other text."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_reviews(dealer_name: str, reviews_csv: str, api_key: str) -> dict:
    """
    Call Claude to analyse review CSV data for dealer_name.
    Returns a dict matching the JSON structure above.
    """
    logger.info(f"[Analyzer] Starting analysis for '{dealer_name}'")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        dealer_name=dealer_name,
        reviews_csv=reviews_csv,
    )

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[
            {"role": "user", "content": prompt}
        ],
        system=SYSTEM_PROMPT,
    )

    raw = message.content[0].text.strip()
    logger.info(f"[Analyzer] Received response ({len(raw)} chars) for '{dealer_name}'")

    # Strip markdown code fences if present
    raw = re.sub(r'^\s*```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```\s*$', '', raw)
    raw = raw.strip()

    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"[Analyzer] JSON parse error: {e}\nRaw: {raw[:500]}")
        # Return a minimal safe structure so the pipeline doesn't die
        analysis = _build_fallback(dealer_name, reviews_csv, str(e))

    # Ensure required keys exist
    analysis.setdefault("dealer_name", dealer_name)
    analysis.setdefault("summary", "Analysis completed.")
    analysis.setdefault("strengths", [])
    analysis.setdefault("weaknesses", [])
    analysis.setdefault("top_themes", [])
    analysis.setdefault("staff_mentions", [])
    analysis.setdefault("sample_positive_reviews", [])
    analysis.setdefault("sample_negative_reviews", [])

    logger.info(f"[Analyzer] Analysis complete for '{dealer_name}'")
    return analysis


def _build_fallback(dealer_name: str, reviews_csv: str, error: str) -> dict:
    """Return a minimal analysis dict when Claude's response can't be parsed."""
    lines = reviews_csv.strip().split("\n")
    total = max(len(lines) - 1, 0)  # subtract header
    return {
        "dealer_name":         dealer_name,
        "total_reviews":       total,
        "average_rating":      0.0,
        "rating_distribution": {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0},
        "summary":             f"Analysis encountered a parsing error: {error}",
        "strengths":           [],
        "weaknesses":          [],
        "top_themes":          [],
        "staff_mentions":      [],
        "recommendation_rate": 0,
        "response_rate":       0,
        "recent_trend":        "stable",
        "key_improvements":    [],
        "sample_positive_reviews": [],
        "sample_negative_reviews": [],
    }
