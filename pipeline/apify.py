"""
pipeline/apify.py - Scrape Google reviews via the Apify Google Maps Reviews actor.
"""

import csv
import io
import time
import logging
import requests

logger = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"


def _start_actor_run(actor_id, api_token, input_payload):
    url = f"{APIFY_BASE}/acts/{actor_id}/runs?token={api_token}"
    resp = requests.post(url, json=input_payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    run_id = data["data"]["id"]
    logger.info(f"[Apify] Started run {run_id} for actor {actor_id}")
    return run_id


def _wait_for_run(run_id, api_token, poll_interval=8, timeout=600):
    url = f"{APIFY_BASE}/actor-runs/{run_id}?token={api_token}"
    elapsed = 0
    while elapsed < timeout:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        status = resp.json()["data"]["status"]
        logger.info(f"[Apify] Run {run_id} status: {status} ({elapsed}s)")
        if status == "SUCCEEDED":
            return
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {run_id} ended with status: {status}")
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Apify run {run_id} did not finish within {timeout}s")


def _fetch_dataset(run_id, api_token):
    url = f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items?token={api_token}&format=json&clean=true"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    items = resp.json()
    logger.info(f"[Apify] Fetched {len(items)} items from run {run_id}")
    return items


def _normalise_review(raw):
    text = (
        raw.get("text") or
        raw.get("reviewText") or
        raw.get("snippet") or
        ""
    )
    rating = raw.get("stars") or raw.get("rating") or raw.get("ratingValue") or 0
    try:
        rating = int(float(rating))
    except (TypeError, ValueError):
        rating = 0

    reviewer = raw.get("reviewer")
    if isinstance(reviewer, dict):
        author = reviewer.get("name") or "Anonymous"
    else:
        author = raw.get("name") or raw.get("author") or "Anonymous"

    date_str = (
        raw.get("publishedAtDate") or
        raw.get("date") or
        raw.get("publishAt") or
        raw.get("time") or
        ""
    )
    if date_str and len(str(date_str)) > 10:
        date_str = str(date_str)[:10]

    return {
        "author": str(author),
        "rating": rating,
        "text":   text.strip(),
        "date":   str(date_str),
    }


def scrape_reviews(place_id, dealer_name, actor_id, api_token, max_reviews=500):
    """Use Apify to scrape Google Maps reviews. Returns list of review dicts."""
    logger.info(f"[Apify] Scraping up to {max_reviews} reviews for '{dealer_name}' ({place_id})")
    input_payload = {
        "placeIds":     [place_id],
        "maxReviews":   max_reviews,
        "reviewsSort":  "newest",
        "language":     "en",
        "personalData": True,
    }
    run_id = _start_actor_run(actor_id, api_token, input_payload)
    _wait_for_run(run_id, api_token)
    raw_items = _fetch_dataset(run_id, api_token)
    reviews = []
    for item in raw_items:
        if "reviews" in item and isinstance(item["reviews"], list):
            for r in item["reviews"]:
                reviews.append(_normalise_review(r))
        else:
            norm = _normalise_review(item)
            if norm["text"] or norm["rating"]:
                reviews.append(norm)
    logger.info(f"[Apify] {len(reviews)} reviews normalised for '{dealer_name}'")
    return reviews


def reviews_to_csv(reviews):
    """Convert a list of review dicts to a CSV string."""
    if not reviews:
        return "author,rating,text,date\n"
    output = io.StringIO()
    fieldnames = ["author", "rating", "text", "date"]
    writer = csv.DictWriter(
        output, fieldnames=fieldnames, extrasaction="ignore",
        quoting=csv.QUOTE_ALL, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(reviews)
    return output.getvalue()
