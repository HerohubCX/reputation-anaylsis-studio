"""
pipeline/generator.py - Generate an HTML reputation dashboard from analysis data.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Star rendering helpers
# ---------------------------------------------------------------------------

def _stars_html(rating, max_stars=5):
    rating = max(0, min(int(round(float(rating or 0))), max_stars))
    filled = "&#9733;" * rating
    empty  = "&#9734;" * (max_stars - rating)
    return f'<span class="stars">{filled}<span class="empty-stars">{empty}</span></span>'


def _bar_width(count, max_count):
    if not max_count:
        return 0
    return round(count / max_count * 100)


def _sentiment_badge(sentiment):
    colour = {"positive": "#22c55e", "negative": "#ef4444", "mixed": "#f59e0b"}.get(sentiment, "#6b7280")
    return f'<span class="badge" style="background:{colour}">{sentiment.title()}</span>'


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{dealer_name} — Reputation Dashboard</title>
<style>
  :root {{
    --primary: #1e40af;
    --accent:  #3b82f6;
    --bg:      #f8fafc;
    --card:    #ffffff;
    --text:    #1e293b;
    --muted:   #64748b;
    --border:  #e2e8f0;
    --pos:     #22c55e;
    --neg:     #ef4444;
    --warn:    #f59e0b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }}
  .hero {{
    background: {bg_image};
    background-size: cover;
    color: #fff;
    padding: 3rem 2rem 2rem;
    text-align: center;
  }}
  .hero h1 {{ font-size: 2.2rem; font-weight: 700; text-shadow: 0 2px 8px rgba(0,0,0,.5); }}
  .hero p  {{ opacity: .85; font-size: 1rem; margin-top: .5rem; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 2rem 1rem; }}
  .grid-4 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: .75rem; padding: 1.25rem 1.5rem;
    box-shadow: 0 1px 4px rgba(0,0,0,.06);
  }}
  .stat-label {{ font-size: .8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}
  .stat-value {{ font-size: 2rem; font-weight: 700; color: var(--primary); }}
  .stars {{ color: #f59e0b; font-size: 1.2em; }}
  .empty-stars {{ color: #d1d5db; }}
  .badge {{
    display: inline-block; padding: .15rem .55rem; border-radius: 999px;
    font-size: .75rem; font-weight: 600; color: #fff;
  }}
  .section {{ margin-bottom: 2rem; }}
  .section h2 {{ font-size: 1.2rem; font-weight: 700; margin-bottom: 1rem; color: var(--primary); border-bottom: 2px solid var(--border); padding-bottom: .4rem; }}
  .bar-row {{ display: flex; align-items: center; gap: .75rem; margin-bottom: .4rem; font-size: .9rem; }}
  .bar-label {{ width: 3rem; text-align: right; color: var(--muted); }}
  .bar-track {{ flex: 1; background: var(--border); border-radius: 999px; height: .55rem; overflow: hidden; }}
  .bar-fill  {{ height: 100%; border-radius: 999px; background: var(--accent); }}
  .bar-count {{ width: 2.5rem; color: var(--muted); font-size: .8rem; }}
  ul.list {{ list-style: none; padding: 0; }}
  ul.list li {{ padding: .35rem 0; border-bottom: 1px solid var(--border); font-size: .9rem; }}
  ul.list li:last-child {{ border-bottom: none; }}
  .themes-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: .6rem; }}
  .theme-chip {{
    background: var(--bg); border: 1px solid var(--border); border-radius: .5rem;
    padding: .5rem .75rem; font-size: .85rem;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .review-card {{ background: var(--bg); border: 1px solid var(--border); border-radius: .5rem; padding: 1rem; margin-bottom: .75rem; }}
  .review-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: .4rem; flex-wrap: wrap; gap: .25rem; }}
  .review-author {{ font-weight: 600; font-size: .9rem; }}
  .review-date   {{ font-size: .8rem; color: var(--muted); }}
  .review-text   {{ font-size: .88rem; color: #334155; line-height: 1.55; }}
  .staff-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: .6rem; }}
  .staff-card {{
    background: var(--bg); border: 1px solid var(--border); border-radius: .5rem;
    padding: .6rem .9rem; font-size: .85rem;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .trend-badge {{
    display: inline-block; padding: .25rem .75rem; border-radius: .4rem;
    font-weight: 700; font-size: 1rem;
  }}
  .improving {{ background: #dcfce7; color: #166534; }}
  .declining {{ background: #fee2e2; color: #991b1b; }}
  .stable    {{ background: #fef9c3; color: #854d0e; }}
  .section-divider {{
    font-size: 1.5rem; font-weight: 700; color: var(--muted);
    margin: 2.5rem 0 1.5rem; padding-top: 1.5rem;
    border-top: 3px solid var(--border); text-align: center;
  }}
  .footer {{ text-align: center; font-size: .78rem; color: var(--muted); padding: 2rem 0 1rem; }}
</style>
</head>
<body>

<div class="hero">
  <h1>{dealer_name}</h1>
  <p>Reputation Dashboard &nbsp;|&nbsp; {date_range_label} &nbsp;|&nbsp; Generated {generated_at}</p>
</div>

<div class="container">

  <!-- KPI strip -->
  <div class="grid-4">
    <div class="card">
      <div class="stat-label">Total Reviews</div>
      <div class="stat-value">{total_reviews}</div>
    </div>
    <div class="card">
      <div class="stat-label">Average Rating</div>
      <div class="stat-value">{average_rating} {stars_html}</div>
    </div>
    <div class="card">
      <div class="stat-label">Recommendation Rate</div>
      <div class="stat-value">{recommendation_rate}%</div>
    </div>
    <div class="card">
      <div class="stat-label">Recent Trend</div>
      <div class="stat-value" style="font-size:1.1rem; padding-top:.4rem">
        <span class="trend-badge {recent_trend}">{recent_trend_title}</span>
      </div>
    </div>
  </div>

  <!-- Summary -->
  <div class="section card" style="margin-bottom:2rem">
    <h2>Executive Summary</h2>
    <p style="font-size:.95rem; line-height:1.7">{summary}</p>
  </div>

  <!-- Rating distribution + Strengths/Weaknesses -->
  <div style="display:grid; grid-template-columns: 1fr 1fr; gap:1.5rem; margin-bottom:2rem; flex-wrap:wrap">
    <div class="card section">
      <h2>Rating Distribution</h2>
      {rating_bars}
    </div>
    <div class="card section">
      <h2>Strengths</h2>
      <ul class="list">{strengths_html}</ul>
      <h2 style="margin-top:1.2rem">Areas for Improvement</h2>
      <ul class="list">{weaknesses_html}</ul>
    </div>
  </div>

  <!-- Top themes -->
  <div class="card section">
    <h2>Top Review Themes</h2>
    <div class="themes-grid">{themes_html}</div>
  </div>

  <!-- Staff mentions -->
  {staff_section}

  <!-- Key improvements -->
  <div class="card section">
    <h2>Recommended Actions</h2>
    <ul class="list">{improvements_html}</ul>
  </div>

  <div class="section-divider">Full Report</div>

  <!-- Sample positive reviews -->
  <div class="section">
    <h2 style="margin-bottom:1rem">&#9733; Highlighted Positive Reviews</h2>
    {positive_reviews_html}
  </div>

  <!-- Sample negative reviews -->
  <div class="section">
    <h2 style="margin-bottom:1rem">&#9744; Highlighted Negative Reviews</h2>
    {negative_reviews_html}
  </div>

</div>

<div class="footer">
  Generated by HeroHub Reputation Studio &nbsp;&bull;&nbsp; {generated_at}
</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Build HTML fragments
# ---------------------------------------------------------------------------

def _rating_bars(dist):
    max_count = max(dist.values(), default=1) or 1
    rows = []
    for star in [5, 4, 3, 2, 1]:
        count = dist.get(str(star), 0)
        width = _bar_width(count, max_count)
        rows.append(
            f'<div class="bar-row">'
            f'<span class="bar-label">{star}&#9733;</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width}%"></div></div>'
            f'<span class="bar-count">{count}</span>'
            f'</div>'
        )
    return "\n".join(rows)


def _list_items(items):
    if not items:
        return '<li style="color:#94a3b8">None identified</li>'
    return "".join(f'<li>{i}</li>' for i in items)


def _themes_html(themes):
    if not themes:
        return '<p style="color:#94a3b8">No themes identified.</p>'
    chips = []
    for t in themes:
        chips.append(
            f'<div class="theme-chip">'
            f'<span>{t.get("theme","")}</span>'
            f'<span style="display:flex;gap:.3rem;align-items:center">'
            f'{_sentiment_badge(t.get("sentiment","mixed"))}'
            f'<span style="font-size:.75rem;color:#64748b">{t.get("count",0)}</span>'
            f'</span></div>'
        )
    return "\n".join(chips)


def _staff_section(staff):
    if not staff:
        return ""
    cards = "".join(
        f'<div class="staff-card">'
        f'<span><strong>{s.get("name","?")}</strong> &times;{s.get("mentions",0)}</span>'
        f'{_sentiment_badge(s.get("sentiment","mixed"))}'
        f'</div>'
        for s in staff
    )
    return (
        f'<div class="card section"><h2>Staff Mentions</h2>'
        f'<div class="staff-grid">{cards}</div></div>'
    )


def _review_card(r):
    return (
        f'<div class="review-card">'
        f'<div class="review-header">'
        f'<span class="review-author">{r.get("author","Anonymous")}</span>'
        f'<span>{_stars_html(r.get("rating",0))}</span>'
        f'<span class="review-date">{r.get("date","")}</span>'
        f'</div>'
        f'<p class="review-text">{r.get("text","").replace("<","&lt;").replace(">","&gt;")}</p>'
        f'</div>'
    )


def _reviews_html(reviews):
    if not reviews:
        return '<p style="color:#94a3b8">No reviews available.</p>'
    return "\n".join(_review_card(r) for r in reviews[:10])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_dashboard(data, bg_image_url, output_dir, hide_chrome=False):
    """
    Render the analysis dict into an HTML dashboard file.
    Returns the absolute path to the generated file.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    dealer_name   = data.get("dealer_name", "Dealership")
    date_range    = data.get("date_range_label", "All Years")
    generated_at  = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    avg            = data.get("average_rating", 0)
    dist           = data.get("rating_distribution", {"5":0,"4":0,"3":0,"2":0,"1":0})
    trend          = data.get("recent_trend", "stable")

    html = DASHBOARD_HTML.format(
        dealer_name          = dealer_name,
        bg_image             = bg_image_url,
        date_range_label     = date_range,
        generated_at         = generated_at,
        total_reviews        = data.get("total_reviews", 0),
        average_rating       = f"{float(avg):.1f}",
        stars_html           = _stars_html(avg),
        recommendation_rate  = data.get("recommendation_rate", 0),
        recent_trend         = trend,
        recent_trend_title   = trend.title(),
        summary              = data.get("summary", ""),
        rating_bars          = _rating_bars(dist),
        strengths_html       = _list_items(data.get("strengths", [])),
        weaknesses_html      = _list_items(data.get("weaknesses", [])),
        themes_html          = _themes_html(data.get("top_themes", [])),
        staff_section        = _staff_section(data.get("staff_mentions", [])),
        improvements_html    = _list_items(data.get("key_improvements", [])),
        positive_reviews_html= _reviews_html(data.get("sample_positive_reviews", [])),
        negative_reviews_html= _reviews_html(data.get("sample_negative_reviews", [])),
    )

    # Sanitise dealer name for filename
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in dealer_name).strip()
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename   = f"{safe_name}_{timestamp}.html"
    filepath   = Path(output_dir) / filename

    filepath.write_text(html, encoding="utf-8")
    logger.info(f"[Generator] Dashboard written to {filepath}")
    return str(filepath)
