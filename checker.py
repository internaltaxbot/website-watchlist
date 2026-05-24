"""
Website Watchlist — Daily Change Checker + Static Site Generator
Checks tracked websites for updates, extracts what changed using text diffs,
saves Wayback Machine snapshots, and generates a static HTML dashboard.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from html import escape

SITES_FILE = Path(__file__).parent / "sites.json"
HASHES_FILE = Path(__file__).parent / "hashes.json"
HISTORY_FILE = Path(__file__).parent / "history.json"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
OUTPUT_DIR = Path(__file__).parent / "public"


def load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else {}


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


def fetch_page(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        for enc in ("utf-8", "latin-1", "ascii"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


def content_hash(text: str) -> str:
    cleaned = " ".join(text.split())
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def extract_text(html: str) -> str:
    """Extract visible text from HTML, stripping tags, scripts, and noise."""
    # Remove script/style/noscript/svg/nav blocks
    text = re.sub(r'<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', ' ', text, flags=re.DOTALL)
    # Remove all tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
    # Remove leftover CSS/JS fragments
    text = re.sub(r'\{[^}]*\}', ' ', text)
    # Normalize whitespace
    lines = [' '.join(line.split()) for line in text.splitlines()]
    lines = [l for l in lines if l and len(l) > 5]
    return '\n'.join(lines)


def is_noise(line: str) -> bool:
    """Filter out navigation, menu items, HTML artifacts, and other junk."""
    noise_patterns = [
        r'aria-',
        r'labelledby',
        r'div\]',
        r'^\s*show\s+submenu',
        r'^\s*hide\s+submenu',
        r'^\s*toggle',
        r'^\s*menu\s*$',
        r'^\s*close\s*$',
        r'^\s*open\s*$',
        r'^\s*search\s*$',
        r'^\s*home\s*$',
        r'^\s*skip to',
        r'^\s*cookie',
        r'^\s*accept all',
        r'^\s*reject all',
        r'\bclass=',
        r'\bstyle=',
        r'\bid=',
        r'\.css',
        r'\.js\b',
        r'\.png\b',
        r'\.jpg\b',
        r'function\s*\(',
        r'var\s+\w',
        r'window\.',
        r'document\.',
        r'xmlns',
    ]
    lower = line.lower().strip()
    # Too short to be meaningful content
    if len(lower) < 10:
        return True
    # Matches noise patterns
    for pat in noise_patterns:
        if re.search(pat, lower):
            return True
    # Mostly non-alphanumeric (likely code/markup fragments)
    alpha_ratio = sum(c.isalpha() or c == ' ' for c in line) / max(len(line), 1)
    if alpha_ratio < 0.4:
        return True
    return False


def get_text_diff(old_text: str, new_text: str) -> dict:
    """Compare old and new text, return added and removed lines."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff = list(unified_diff(old_lines, new_lines, lineterm=''))

    added = []
    removed = []
    for line in diff:
        if line.startswith('+') and not line.startswith('+++'):
            clean = line[1:].strip()
            if clean and len(clean) > 3 and not is_noise(clean):
                added.append(clean)
        elif line.startswith('-') and not line.startswith('---'):
            clean = line[1:].strip()
            if clean and len(clean) > 3 and not is_noise(clean):
                removed.append(clean)

    return {"added": added[:20], "removed": removed[:20]}


def summarize_changes(diff: dict, label: str) -> str:
    """Create a human-readable summary of what changed."""
    added = diff.get("added", [])
    removed = diff.get("removed", [])

    if not added and not removed:
        return "Minor formatting or structural changes detected."

    parts = []
    if added:
        significant = sorted(added, key=len, reverse=True)[:5]
        parts.append("New content added:\n" + "\n".join(f"  + {line[:150]}" for line in significant))
    if removed:
        significant = sorted(removed, key=len, reverse=True)[:3]
        parts.append("Content removed:\n" + "\n".join(f"  - {line[:150]}" for line in significant))

    return "\n\n".join(parts)


def request_wayback_snapshot(url: str) -> str | None:
    """Request the Wayback Machine to save a snapshot."""
    try:
        save_url = f"https://web.archive.org/save/{url}"
        req = Request(save_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; WatchlistBot/1.0)"
        })
        with urlopen(req, timeout=60) as resp:
            archive_path = resp.headers.get("Content-Location") or resp.headers.get("Location")
            if archive_path:
                return f"https://web.archive.org{archive_path}"
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            return f"https://web.archive.org/web/{ts}/{url}"
    except Exception as e:
        print(f"  -> Wayback save failed: {e}")
        return f"https://web.archive.org/web/*/{url}"


def url_to_filename(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def get_favicon(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname
        return f"https://www.google.com/s2/favicons?domain={host}&sz=32"
    except Exception:
        return ""


def get_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname.replace("www.", "")
    except Exception:
        return url


def check_sites() -> tuple[list[dict], list[dict]]:
    sites = load_json(SITES_FILE, [])
    hashes = load_json(HASHES_FILE, {})
    history = load_json(HISTORY_FILE, [])
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    statuses = []
    new_changes = []

    for site in sites:
        url = site["url"]
        label = site.get("label", get_domain(url))
        print(f"Checking: {label} ({url})")

        status = {
            "url": url,
            "label": label,
            "favicon": get_favicon(url),
            "domain": get_domain(url),
            "lastChecked": now,
            "status": "unchanged",
            "error": None,
            "summary": None,
        }

        try:
            html = fetch_page(url)
            new_hash = content_hash(html)
            old_hash = hashes.get(url)
            new_text = extract_text(html)
            snapshot_file = SNAPSHOTS_DIR / f"{url_to_filename(url)}.txt"

            if old_hash is None:
                print(f"  -> First check, storing baseline.")
                hashes[url] = new_hash
                snapshot_file.write_text(new_text, encoding="utf-8")
                status["status"] = "new"

            elif new_hash != old_hash:
                print(f"  -> CHANGED!")

                old_text = ""
                if snapshot_file.exists():
                    old_text = snapshot_file.read_text(encoding="utf-8")

                diff = get_text_diff(old_text, new_text)
                summary = summarize_changes(diff, label)

                print(f"  -> Requesting Wayback Machine snapshot...")
                wayback_url = request_wayback_snapshot(url)

                hashes[url] = new_hash
                snapshot_file.write_text(new_text, encoding="utf-8")

                status["status"] = "changed"
                status["summary"] = summary

                change_entry = {
                    "url": url,
                    "label": label,
                    "time": now,
                    "type": "changed",
                    "summary": summary,
                    "wayback_url": wayback_url,
                    "added_count": len(diff["added"]),
                    "removed_count": len(diff["removed"]),
                    "added_preview": [line[:200] for line in diff["added"][:5]],
                }
                new_changes.append(change_entry)
                history.insert(0, change_entry)
            else:
                print(f"  -> No change.")

        except (URLError, OSError, Exception) as e:
            print(f"  -> Error: {e}")
            status["status"] = "error"
            status["error"] = str(e)[:120]

        statuses.append(status)

    history = history[:200]
    save_json(HASHES_FILE, hashes)
    save_json(HISTORY_FILE, history)
    return statuses, new_changes


def format_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y at %H:%M UTC")
    except Exception:
        return iso_str


def build_html(statuses: list[dict], history: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%B %d, %Y at %H:%M UTC")
    changed_count = sum(1 for s in statuses if s["status"] == "changed")
    total = len(statuses)

    site_cards = ""
    for s in statuses:
        if s["status"] == "changed":
            border_color = "#c8a438"
            badge = '<span class="badge badge-changed">UPDATED</span>'
            glow = "box-shadow: 0 0 24px rgba(200,164,56,0.1);"
        elif s["status"] == "error":
            border_color = "#8b3a3a"
            badge = '<span class="badge badge-error">ERROR</span>'
            glow = ""
        elif s["status"] == "new":
            border_color = "#3a6b5a"
            badge = '<span class="badge badge-new">BASELINE SET</span>'
            glow = ""
        else:
            border_color = "#2a2a30"
            badge = '<span class="badge badge-ok">NO CHANGE</span>'
            glow = ""

        error_line = ""
        if s["error"]:
            error_line = f'<div class="error-msg">{escape(s["error"])}</div>'

        summary_block = ""
        if s.get("summary"):
            summary_block = f'<div class="change-summary">{escape(s["summary"])}</div>'

        site_cards += f"""
        <div class="card" style="border-color:{border_color};{glow}">
          <div class="card-row">
            <div class="card-icon">
              <img src="{escape(s['favicon'])}" alt="" width="20" height="20" onerror="this.style.display='none'" />
            </div>
            <div class="card-body">
              <div class="card-header">
                <a href="{escape(s['url'])}" target="_blank" rel="noopener" class="card-title">{escape(s['label'])}</a>
                {badge}
              </div>
              <div class="card-domain">{escape(s['domain'])}</div>
              {error_line}
              {summary_block}
            </div>
            <a href="{escape(s['url'])}" target="_blank" rel="noopener" class="visit-btn" title="Visit site">&#x2197;</a>
          </div>
        </div>
        """

    history_rows = ""
    for h in history[:30]:
        summary = h.get("summary", "")
        wayback = h.get("wayback_url", "")
        added_count = h.get("added_count", 0)
        removed_count = h.get("removed_count", 0)
        added_preview = h.get("added_preview", [])

        stats_parts = []
        if added_count:
            stats_parts.append(f'<span class="diff-added">+{added_count} added</span>')
        if removed_count:
            stats_parts.append(f'<span class="diff-removed">-{removed_count} removed</span>')
        stats_html = " ".join(stats_parts)

        preview_html = ""
        if added_preview:
            preview_items = "".join(
                f'<div class="preview-line">&bull; {escape(line[:180])}</div>'
                for line in added_preview[:3]
            )
            preview_html = f'<div class="preview-block">{preview_items}</div>'

        links = f'<a href="{escape(h["url"])}" target="_blank" rel="noopener" class="history-link">Visit site &#x2197;</a>'
        if wayback:
            links += f' <a href="{escape(wayback)}" target="_blank" rel="noopener" class="history-link wayback-link">Wayback snapshot &#x2197;</a>'

        history_rows += f"""
        <div class="history-entry">
          <div class="history-entry-header">
            <div class="history-dot"></div>
            <a href="{escape(h['url'])}" target="_blank" rel="noopener" class="history-label">{escape(h['label'])}</a>
            <span class="history-time">{format_time(h['time'])}</span>
          </div>
          <div class="history-detail">
            <div class="diff-stats">{stats_html}</div>
            {preview_html}
            <div class="history-links">{links}</div>
          </div>
        </div>
        """

    if not history_rows:
        history_rows = '<div class="empty-state">No changes recorded yet. Check back after the next scan.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Watchlist &mdash; Site Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=IBM+Plex+Mono:wght@400;500;600&family=Sora:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #08080a;
      --surface: #111114;
      --surface2: #18181c;
      --border: #232328;
      --amber: #c8a438;
      --amber-dim: rgba(200,164,56,0.12);
      --amber-glow: rgba(200,164,56,0.06);
      --text: #ddd9d0;
      --text-dim: #807c74;
      --text-muted: #4e4b46;
      --green: #4a9;
      --red: #b54;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Sora', sans-serif;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}
    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      opacity: 0.03;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
      pointer-events: none;
      z-index: 9999;
    }}
    .container {{ max-width: 720px; margin: 0 auto; padding: 48px 24px 80px; }}
    .header {{ margin-bottom: 48px; animation: fadeIn 0.6s ease; }}
    .header-row {{ display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }}
    .pulse-dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--amber); box-shadow: 0 0 12px var(--amber); animation: pulse 2.5s ease-in-out infinite; }}
    h1 {{ font-family: 'DM Serif Display', serif; font-size: 32px; font-weight: 400; letter-spacing: -0.5px; }}
    .subtitle {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-muted); letter-spacing: 0.5px; }}
    .stats {{ display: flex; gap: 20px; margin-top: 20px; flex-wrap: wrap; }}
    .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; min-width: 120px; }}
    .stat-value {{ font-family: 'IBM Plex Mono', monospace; font-size: 22px; font-weight: 600; color: var(--amber); }}
    .stat-label {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 1px; font-family: 'IBM Plex Mono', monospace; }}
    .section-title {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--text-muted); margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
    .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 10px; transition: background 0.2s; animation: slideUp 0.4s ease backwards; }}
    .card:hover {{ background: var(--surface2); }}
    .card-row {{ display: flex; align-items: flex-start; gap: 12px; }}
    .card-icon {{ width: 36px; height: 36px; border-radius: 8px; background: var(--amber-dim); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
    .card-body {{ flex: 1; min-width: 0; }}
    .card-header {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .card-title {{ color: var(--text); text-decoration: none; font-weight: 600; font-size: 14px; }}
    .card-title:hover {{ color: var(--amber); }}
    .card-domain {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-dim); margin-top: 3px; }}
    .badge {{ font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 4px; letter-spacing: 0.5px; text-transform: uppercase; }}
    .badge-changed {{ background: var(--amber-dim); color: var(--amber); }}
    .badge-ok {{ background: rgba(255,255,255,0.03); color: var(--text-muted); }}
    .badge-new {{ background: rgba(68,170,153,0.12); color: var(--green); }}
    .badge-error {{ background: rgba(187,85,68,0.12); color: var(--red); }}
    .error-msg {{ font-size: 11px; color: var(--red); margin-top: 6px; font-family: 'IBM Plex Mono', monospace; }}
    .change-summary {{ margin-top: 10px; padding: 10px 12px; background: var(--amber-glow); border-left: 2px solid var(--amber); border-radius: 0 6px 6px 0; font-size: 12px; color: var(--text-dim); line-height: 1.6; white-space: pre-wrap; font-family: 'IBM Plex Mono', monospace; }}
    .visit-btn {{ width: 32px; height: 32px; border-radius: 6px; border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; color: var(--text-dim); text-decoration: none; font-size: 16px; flex-shrink: 0; transition: all 0.2s; }}
    .visit-btn:hover {{ border-color: var(--amber); color: var(--amber); background: var(--amber-dim); }}
    .history-section {{ margin-top: 48px; }}
    .history-entry {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 10px; animation: slideUp 0.3s ease backwards; }}
    .history-entry:hover {{ background: var(--surface2); }}
    .history-entry-header {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    .history-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--amber); flex-shrink: 0; }}
    .history-label {{ font-size: 14px; font-weight: 600; color: var(--text); text-decoration: none; flex: 1; }}
    .history-label:hover {{ color: var(--amber); }}
    .history-time {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-muted); white-space: nowrap; }}
    .history-detail {{ margin-top: 10px; padding-left: 18px; }}
    .diff-stats {{ display: flex; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }}
    .diff-added {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--green); background: rgba(68,170,153,0.1); padding: 2px 8px; border-radius: 4px; }}
    .diff-removed {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--red); background: rgba(187,85,68,0.1); padding: 2px 8px; border-radius: 4px; }}
    .preview-block {{ background: rgba(200,164,56,0.05); border-left: 2px solid rgba(200,164,56,0.3); border-radius: 0 6px 6px 0; padding: 10px 12px; margin-bottom: 10px; }}
    .preview-line {{ font-size: 12px; color: var(--text-dim); line-height: 1.6; margin-bottom: 4px; font-family: 'IBM Plex Mono', monospace; }}
    .preview-line:last-child {{ margin-bottom: 0; }}
    .history-links {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 4px; }}
    .history-link {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; color: var(--amber); text-decoration: none; padding: 4px 10px; border: 1px solid rgba(200,164,56,0.25); border-radius: 5px; transition: all 0.2s; }}
    .history-link:hover {{ background: var(--amber-dim); border-color: var(--amber); }}
    .wayback-link {{ color: #7eb8da; border-color: rgba(126,184,218,0.25); }}
    .wayback-link:hover {{ background: rgba(126,184,218,0.1); border-color: #7eb8da; }}
    .empty-state {{ text-align: center; color: var(--text-muted); font-family: 'IBM Plex Mono', monospace; font-size: 12px; padding: 32px 0; }}
    .footer {{ text-align: center; margin-top: 60px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-muted); line-height: 1.8; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(-12px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    @keyframes slideUp {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; box-shadow: 0 0 12px var(--amber); }} 50% {{ opacity: 0.5; box-shadow: 0 0 4px var(--amber); }} }}
    .card:nth-child(1) {{ animation-delay: 0.05s; }}
    .card:nth-child(2) {{ animation-delay: 0.1s; }}
    .card:nth-child(3) {{ animation-delay: 0.15s; }}
    .card:nth-child(4) {{ animation-delay: 0.2s; }}
    .card:nth-child(5) {{ animation-delay: 0.25s; }}
    .history-entry:nth-child(1) {{ animation-delay: 0.05s; }}
    .history-entry:nth-child(2) {{ animation-delay: 0.1s; }}
    .history-entry:nth-child(3) {{ animation-delay: 0.15s; }}
    .history-entry:nth-child(4) {{ animation-delay: 0.2s; }}
    .history-entry:nth-child(5) {{ animation-delay: 0.25s; }}
    @media (max-width: 520px) {{
      .container {{ padding: 28px 16px 60px; }}
      h1 {{ font-size: 24px; }}
      .stats {{ gap: 10px; }}
      .stat {{ min-width: 100px; padding: 10px 14px; }}
      .stat-value {{ font-size: 18px; }}
      .history-detail {{ padding-left: 8px; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="header-row">
        <div class="pulse-dot"></div>
        <h1>Watchlist</h1>
      </div>
      <div class="subtitle">Last scan: {now_str}</div>
      <div class="stats">
        <div class="stat">
          <div class="stat-value">{total}</div>
          <div class="stat-label">Tracked</div>
        </div>
        <div class="stat">
          <div class="stat-value">{changed_count}</div>
          <div class="stat-label">Updated</div>
        </div>
        <div class="stat">
          <div class="stat-value">{len(history)}</div>
          <div class="stat-label">All-time changes</div>
        </div>
      </div>
    </div>
    <div class="section-title">Monitored Sites</div>
    {site_cards}
    <div class="history-section">
      <div class="section-title">Change History</div>
      {history_rows}
    </div>
    <div class="footer">
      Auto-updated daily via GitHub Actions<br />
      Share this page with anyone &mdash; it's public
    </div>
  </div>
</body>
</html>"""


def main():
    print(f"=== Watchlist Check: {datetime.now(timezone.utc).isoformat()} ===\n")

    if not SITES_FILE.exists():
        print("No sites.json found. See README.md.")
        sys.exit(1)

    history = load_json(HISTORY_FILE, [])
    statuses, new_changes = check_sites()
    history = load_json(HISTORY_FILE, [])

    print(f"\nGenerating dashboard...")
    OUTPUT_DIR.mkdir(exist_ok=True)
    html = build_html(statuses, history)
    (OUTPUT_DIR / "index.html").write_text(html)

    if new_changes:
        print(f"\n{len(new_changes)} change(s) detected.")
    else:
        print(f"\nNo changes detected.")
    print(f"Dashboard written to public/index.html")


if __name__ == "__main__":
    main()
