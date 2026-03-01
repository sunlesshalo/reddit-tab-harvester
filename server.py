#!/usr/bin/env python3
"""Tab Harvester — Local server that fetches Reddit content and runs Claude analysis."""

import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock

PORT = 7777
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PROMPT_FILE = BASE_DIR / "prompt.txt"

DATA_DIR.mkdir(exist_ok=True)
KB_PATH = DATA_DIR / "knowledge.json"


def _load_kb():
    """Load knowledge base from disk."""
    if KB_PATH.exists():
        return json.loads(KB_PATH.read_text())
    return {"posts": {}}


def _save_kb(kb):
    """Atomic write knowledge base to disk."""
    tmp = KB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(kb, indent=2))
    tmp.rename(KB_PATH)


def _add_to_kb(merged_posts, session_ts):
    """Add merged posts to knowledge base, skipping duplicates."""
    kb = _load_kb()
    added = 0
    for p in merged_posts:
        url = p.get("url", "")
        if not url or url in kb["posts"]:
            continue
        kb["posts"][url] = {
            **p,
            "harvested_at": datetime.now().isoformat(),
            "harvest_session": session_ts,
        }
        added += 1
    _save_kb(kb)
    print(f"[kb] Added {added} posts, {len(merged_posts) - added} dupes skipped. Total: {len(kb['posts'])}")
    return added


# Rate limiter for Reddit API
_last_fetch_time = 0
_fetch_lock = Lock()


def _rate_limit():
    """Ensure at least 1s between Reddit requests (thread-safe)."""
    global _last_fetch_time
    with _fetch_lock:
        now = time.time()
        wait = max(0, 1.0 - (now - _last_fetch_time))
        if wait > 0:
            time.sleep(wait)
        _last_fetch_time = time.time()


def _extract_comments(comments_raw):
    """Extract top comments from Reddit comment listing."""
    top_comments = []
    for c in comments_raw[:5]:
        if c["kind"] != "t1":
            continue
        cd = c["data"]
        top_comments.append({
            "author": cd.get("author", "[deleted]"),
            "score": cd.get("score", 0),
            "body": cd.get("body", ""),
        })
    return top_comments


def fetch_reddit_url(url):
    """Fetch a Reddit URL via the .json API. Handles posts, subreddits, and listings."""
    stripped = url.rstrip("/")
    if stripped in ("https://www.reddit.com", "https://reddit.com", "http://www.reddit.com"):
        return {"url": url, "error": "homepage — nothing to fetch"}

    _rate_limit()

    json_url = stripped + ".json"
    req = urllib.request.Request(
        json_url,
        headers={"User-Agent": "TabHarvester/1.0 (local; educational)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, Exception) as e:
        return {"url": url, "error": str(e)}

    # Case 1: Post page — Reddit returns [post_listing, comments_listing]
    if isinstance(raw, list) and len(raw) >= 1:
        post_data = raw[0]["data"]["children"][0]["data"]
        comments_raw = raw[1]["data"]["children"] if len(raw) > 1 else []
        return {
            "url": url,
            "title": post_data.get("title", ""),
            "selftext": post_data.get("selftext", ""),
            "score": post_data.get("score", 0),
            "subreddit": post_data.get("subreddit", ""),
            "num_comments": post_data.get("num_comments", 0),
            "created_utc": post_data.get("created_utc", 0),
            "top_comments": _extract_comments(comments_raw),
        }

    # Case 2: Subreddit/listing page
    if isinstance(raw, dict) and raw.get("kind") == "Listing":
        children = raw.get("data", {}).get("children", [])
        posts = []
        for child in children[:10]:
            d = child.get("data", {})
            posts.append({
                "title": d.get("title", ""),
                "selftext": d.get("selftext", "")[:500],
                "score": d.get("score", 0),
                "subreddit": d.get("subreddit", ""),
                "num_comments": d.get("num_comments", 0),
                "permalink": d.get("permalink", ""),
            })
        subreddit = posts[0]["subreddit"] if posts else "unknown"
        return {
            "url": url,
            "title": f"Subreddit listing: r/{subreddit} (top {len(posts)} posts)",
            "selftext": "\n\n".join(
                f"• [{p['score']} pts] {p['title']}" + (f"\n  {p['selftext'][:200]}" if p['selftext'] else "")
                for p in posts
            ),
            "score": sum(p["score"] for p in posts),
            "subreddit": subreddit,
            "num_comments": sum(p["num_comments"] for p in posts),
            "created_utc": 0,
            "top_comments": [],
        }

    return {"url": url, "error": "unexpected format"}


def fetch_all_parallel(urls, progress_cb=None):
    """Fetch all Reddit URLs in parallel (3 threads, rate-limited)."""
    results = [None] * len(urls)
    done_count = 0

    def _fetch(idx, url):
        return idx, fetch_reddit_url(url)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch, i, u): i for i, u in enumerate(urls)}
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result
            done_count += 1
            ok = "error" not in result
            title = result.get("title", "")[:50] if ok else result.get("error", "")[:50]
            print(f"[fetch] {done_count}/{len(urls)} {'OK' if ok else 'ERR'}: {title}")
            if progress_cb:
                progress_cb("fetch", done_count, len(urls))

    return results


def format_content_for_prompt(posts):
    """Format fetched posts as plain text for the Claude prompt."""
    lines = []
    for i, p in enumerate(posts, 1):
        if "error" in p:
            continue  # Skip failed fetches — don't waste tokens
        lines.append(f"\n--- POST {i} ---")
        lines.append(f"URL: {p['url']}")
        lines.append(f"Title: {p['title']}")
        lines.append(f"Subreddit: r/{p['subreddit']}")
        lines.append(f"Score: {p['score']} | Comments: {p['num_comments']}")
        if p["selftext"]:
            lines.append(f"\nPost body:\n{p['selftext']}")
        if p["top_comments"]:
            lines.append("\nTop comments:")
            for c in p["top_comments"]:
                lines.append(f"  [{c['score']} pts] u/{c['author']}: {c['body'][:500]}")
    return "\n".join(lines)


def strip_code_fences(text):
    """Remove markdown code fences (```json ... ```) from Claude's response."""
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def run_claude(prompt_text):
    """Run claude CLI in print mode and return the output."""
    try:
        print(f"[claude] Sending {len(prompt_text)} chars to claude -p (haiku)...")
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json", "--model", "haiku"],
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=300,
            cwd="/tmp",
        )
        if result.returncode != 0:
            print(f"[claude] ERROR exit {result.returncode}: {result.stderr[:300]}")
            return {"error": f"claude exited {result.returncode}: {result.stderr[:500]}"}

        debug_path = DATA_DIR / f"debug-claude-{datetime.now().strftime('%H%M%S')}.txt"
        debug_path.write_text(result.stdout[:5000])
        print(f"[claude] Response saved to {debug_path}")

        try:
            wrapper = json.loads(result.stdout)
            content = wrapper.get("result", result.stdout)
        except json.JSONDecodeError:
            content = result.stdout

        content = strip_code_fences(content)
        print(f"[claude] After strip: {content[:150]}...")

        try:
            parsed = json.loads(content)
            print(f"[claude] OK: {len(parsed.get('posts', []))} posts, {len(parsed.get('summary', []))} themes")
            return parsed
        except json.JSONDecodeError as e:
            print(f"[claude] JSON parse failed: {e}")
            return {"raw_text": content}
    except subprocess.TimeoutExpired:
        return {"error": "claude timed out after 5 minutes"}
    except FileNotFoundError:
        return {"error": "claude CLI not found — is it installed and in PATH?"}


def merge_analysis_with_content(analysis, fetched_posts):
    """Merge Claude's lightweight analysis with the server's full fetched content."""
    analysis_by_idx = {}
    for ap in analysis.get("posts", []):
        analysis_by_idx[ap.get("post_index", 0)] = ap

    merged = []
    post_num = 0
    for fp in fetched_posts:
        if "error" in fp:
            continue
        post_num += 1
        ap = analysis_by_idx.get(post_num, {})
        merged.append({
            "title": fp.get("title", "Untitled"),
            "url": fp.get("url", ""),
            "subreddit": fp.get("subreddit", "?"),
            "score": fp.get("score", 0),
            "num_comments": fp.get("num_comments", 0),
            "selftext": fp.get("selftext", ""),
            "top_comments": fp.get("top_comments", []),
            "category": ap.get("category", "Other"),
            "one_liner": ap.get("one_liner", ""),
            "relevance": ap.get("relevance", 3),
        })
    return merged


def build_digest_html(analysis, fetched_posts, post_count, timestamp):
    """Build digest HTML. Merges Claude's analysis with fetched content."""
    summary = analysis.get("summary", [])
    posts = merge_analysis_with_content(analysis, fetched_posts)
    posts.sort(key=lambda p: -p["relevance"])

    categories = {}
    for p in posts:
        categories.setdefault(p["category"], []).append(p)

    summary_html = "".join(f"<li>{s}</li>" for s in summary)

    quick_scan = ""
    for cat, cat_posts in categories.items():
        items = "".join(
            f'<li><strong>{p["title"]}</strong> '
            f'<span class="meta">r/{p["subreddit"]} · {p["score"]} pts</span>'
            f'<br><span class="one-liner">{p["one_liner"]}</span></li>'
            for p in cat_posts
        )
        quick_scan += f"<h3>{cat} ({len(cat_posts)})</h3><ul>{items}</ul>"

    deep_read = ""
    for i, p in enumerate(posts):
        if p.get("top_comments"):
            comments = "".join(
                f"<li><strong>[{c['score']} pts]</strong> u/{c['author']}: {c['body'][:600]}</li>"
                for c in p["top_comments"]
            )
        else:
            comments = "<li>No comments captured</li>"

        full_text = (p.get("selftext") or "No content").replace("\n", "<br>")
        deep_read += f"""
        <div class="post" id="post-{i}">
            <h3>{p["title"]}</h3>
            <div class="meta">
                <a href="{p['url']}" target="_blank">Source</a> ·
                r/{p["subreddit"]} · {p["score"]} pts ·
                {p["num_comments"]} comments ·
                Relevance: {p["relevance"]}/5
            </div>
            <p class="one-liner">{p["one_liner"]}</p>
            <details>
                <summary>Full post + comments</summary>
                <div class="full-text">{full_text}</div>
                <h4>Top Comments</h4>
                <ul class="comments">{comments}</ul>
            </details>
        </div>"""

    if "raw_text" in analysis:
        raw = analysis["raw_text"].replace("\n", "<br>")
        quick_scan = f"<div class='raw'>{raw}</div>"
        deep_read = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Reddit Digest — {timestamp}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #ff6b35; border-bottom: 2px solid #333; padding-bottom: 10px; }}
  h2 {{ color: #c4c4c4; margin-top: 30px; }}
  h3 {{ color: #ff9f1c; }}
  a {{ color: #4ecdc4; }}
  .meta {{ color: #888; font-size: 0.9em; }}
  .one-liner {{ color: #bbb; font-style: italic; }}
  .post {{ background: #16213e; padding: 15px; margin: 15px 0; border-radius: 8px; border-left: 3px solid #ff6b35; }}
  .full-text {{ background: #0f3460; padding: 12px; border-radius: 4px; margin: 10px 0; line-height: 1.6; }}
  .comments {{ background: #1a1a2e; padding: 10px; border-radius: 4px; }}
  .comments li {{ margin: 8px 0; }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; color: #4ecdc4; font-weight: bold; }}
  ul {{ line-height: 1.8; }}
  .raw {{ white-space: pre-wrap; background: #16213e; padding: 15px; border-radius: 8px; }}
  @media print {{ body {{ background: white; color: black; }} .post {{ border-color: #333; background: #f5f5f5; }} }}
</style>
</head>
<body>
<h1>Reddit Digest — {timestamp}</h1>
<p>{post_count} tabs harvested</p>

<h2>Key Themes</h2>
<ul>{summary_html}</ul>

<h2>Quick Scan</h2>
{quick_scan}

<h2>Deep Read</h2>
<p><em>Click any post to expand full content + comments.</em></p>
{deep_read}

</body>
</html>"""


def build_knowledge_html(posts):
    """Build the consolidated knowledge base page with filtering and dismiss."""
    posts_json = json.dumps(posts)
    count = len(posts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Knowledge Base — Tab Harvester</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #ff6b35; margin-bottom: 4px; }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 20px; }}
  .filters {{ position: sticky; top: 0; background: #1a1a2e; padding: 12px 0; border-bottom: 1px solid #333; z-index: 10; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .filters button {{ padding: 6px 14px; border: 1px solid #444; border-radius: 16px; background: transparent; color: #ccc; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
  .filters button:hover {{ border-color: #ff6b35; color: #ff6b35; }}
  .filters button.active {{ background: #ff6b35; border-color: #ff6b35; color: white; }}
  .filters select {{ padding: 6px 10px; border: 1px solid #444; border-radius: 8px; background: #16213e; color: #ccc; font-size: 13px; margin-left: auto; }}
  .post {{ background: #16213e; padding: 15px; margin: 12px 0; border-radius: 8px; border-left: 3px solid #ff6b35; position: relative; transition: opacity 0.3s, transform 0.3s; }}
  .post.dismissed {{ opacity: 0; transform: translateX(50px); pointer-events: none; }}
  .post h3 {{ color: #e0e0e0; font-size: 15px; padding-right: 30px; }}
  .post h3 a {{ color: #e0e0e0; text-decoration: none; }}
  .post h3 a:hover {{ color: #4ecdc4; }}
  .meta {{ color: #888; font-size: 12px; margin: 4px 0; }}
  .meta a {{ color: #4ecdc4; text-decoration: none; }}
  .one-liner {{ color: #bbb; font-style: italic; font-size: 14px; margin: 6px 0; }}
  .dismiss {{ position: absolute; top: 12px; right: 12px; width: 26px; height: 26px; border: none; border-radius: 50%; background: transparent; color: #666; cursor: pointer; font-size: 16px; line-height: 26px; text-align: center; transition: all 0.2s; }}
  .dismiss:hover {{ background: #ff4444; color: white; }}
  .relevance {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
  .rel-5 {{ background: #2d5a27; color: #8bc34a; }}
  .rel-4 {{ background: #1a4a2e; color: #66bb6a; }}
  .rel-3 {{ background: #3a3a1a; color: #fdd835; }}
  .rel-2 {{ background: #3a2a1a; color: #ffb74d; }}
  .rel-1 {{ background: #3a1a1a; color: #ef5350; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #4ecdc4; font-weight: 600; font-size: 13px; }}
  .full-text {{ background: #0f3460; padding: 12px; border-radius: 4px; margin: 8px 0; line-height: 1.6; font-size: 14px; }}
  .comments {{ padding: 8px; font-size: 13px; }}
  .comments li {{ margin: 6px 0; list-style: none; }}
  .empty {{ text-align: center; color: #666; margin-top: 60px; }}
  .empty p {{ font-size: 18px; margin-bottom: 8px; }}
  .cat-tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; background: #333; color: #aaa; margin-right: 6px; }}
</style>
</head>
<body>

<h1>Knowledge Base</h1>
<p class="subtitle"><span id="count">{count}</span> posts collected</p>

<div class="filters">
  <button class="active" data-cat="all">All</button>
  <button data-cat="Ideas">Ideas</button>
  <button data-cat="Methods">Methods</button>
  <button data-cat="Tools">Tools</button>
  <button data-cat="Discussion">Discussion</button>
  <button data-cat="Reference">Reference</button>
  <select id="sort-select">
    <option value="relevance">Sort: Relevance</option>
    <option value="date">Sort: Newest</option>
    <option value="score">Sort: Score</option>
  </select>
</div>

<div id="posts-container"></div>

<div class="empty" id="empty-state" style="display:none">
  <p>No posts yet</p>
  <span>Harvest some Reddit tabs to get started.</span>
</div>

<script>
const KB_DATA = {posts_json};
let currentFilter = "all";
let currentSort = "relevance";

function escapeHtml(s) {{
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}}

function renderPosts() {{
  let posts = [...KB_DATA];

  if (currentFilter !== "all") {{
    posts = posts.filter(p => p.category === currentFilter);
  }}

  if (currentSort === "relevance") posts.sort((a, b) => (b.relevance || 0) - (a.relevance || 0));
  else if (currentSort === "date") posts.sort((a, b) => (b.harvested_at || "").localeCompare(a.harvested_at || ""));
  else if (currentSort === "score") posts.sort((a, b) => (b.score || 0) - (a.score || 0));

  const container = document.getElementById("posts-container");
  const empty = document.getElementById("empty-state");

  if (posts.length === 0) {{
    container.innerHTML = "";
    empty.style.display = "block";
    return;
  }}
  empty.style.display = "none";

  container.innerHTML = posts.map(p => {{
    const rel = p.relevance || 3;
    const comments = (p.top_comments || []).map(c =>
      `<li><strong>[${{c.score}} pts]</strong> u/${{escapeHtml(c.author)}}: ${{escapeHtml((c.body || "").slice(0, 600))}}</li>`
    ).join("") || "<li>No comments</li>";
    const fullText = escapeHtml(p.selftext || "No content").replace(/\\n/g, "<br>");
    const date = p.harvested_at ? new Date(p.harvested_at).toLocaleDateString() : "";

    return `
      <div class="post" data-url="${{escapeHtml(p.url)}}" data-cat="${{p.category}}">
        <button class="dismiss" title="Dismiss" onclick="dismissPost(this)">&times;</button>
        <h3><a href="${{p.url}}" target="_blank">${{escapeHtml(p.title)}}</a></h3>
        <div class="meta">
          <span class="cat-tag">${{p.category}}</span>
          <span class="relevance rel-${{rel}}">${{rel}}/5</span>
          &middot; r/${{escapeHtml(p.subreddit)}} &middot; ${{p.score}} pts &middot; ${{p.num_comments}} comments
          &middot; ${{date}}
        </div>
        <p class="one-liner">${{escapeHtml(p.one_liner)}}</p>
        <details>
          <summary>Full post + comments</summary>
          <div class="full-text">${{fullText}}</div>
          <h4 style="color:#888;font-size:13px;margin:8px 0 4px">Top Comments</h4>
          <ul class="comments">${{comments}}</ul>
        </details>
      </div>`;
  }}).join("");
}}

async function dismissPost(btn) {{
  const card = btn.closest(".post");
  const url = card.dataset.url;
  card.classList.add("dismissed");

  try {{
    const resp = await fetch("/knowledge/dismiss", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ url }})
    }});
    if (resp.ok) {{
      const idx = KB_DATA.findIndex(p => p.url === url);
      if (idx !== -1) KB_DATA.splice(idx, 1);
      setTimeout(() => {{
        card.remove();
        document.getElementById("count").textContent = KB_DATA.length;
        if (KB_DATA.length === 0) document.getElementById("empty-state").style.display = "block";
      }}, 300);
    }}
  }} catch (e) {{
    card.classList.remove("dismissed");
  }}
}}

// Filter buttons
document.querySelectorAll(".filters button").forEach(btn => {{
  btn.addEventListener("click", () => {{
    document.querySelectorAll(".filters button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.cat;
    renderPosts();
  }});
}});

// Sort select
document.getElementById("sort-select").addEventListener("change", (e) => {{
  currentSort = e.target.value;
  renderPosts();
}});

renderPosts();
</script>
</body>
</html>"""


class HarvestHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, event, data):
        """Send a Server-Sent Event line."""
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(msg.encode())
        self.wfile.flush()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True})
        elif self.path.startswith("/digest/"):
            filename = self.path.split("/digest/", 1)[1]
            filepath = DATA_DIR / filename
            if filepath.exists() and filepath.suffix == ".html":
                self._send_html(filepath.read_text())
            else:
                self._send_json({"error": "not found"}, 404)
        elif self.path == "/digests":
            files = sorted(DATA_DIR.glob("*.html"), reverse=True)
            self._send_json([f.name for f in files[:20]])
        elif self.path == "/knowledge":
            kb = _load_kb()
            posts = sorted(kb["posts"].values(), key=lambda p: -p.get("relevance", 3))
            self._send_html(build_knowledge_html(posts))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/harvest":
            self._handle_harvest_json()
        elif self.path == "/harvest-stream":
            self._handle_harvest_sse()
        elif self.path == "/knowledge/dismiss":
            self._handle_knowledge_dismiss()
        else:
            self._send_json({"error": "not found"}, 404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def _run_harvest(self, urls, progress_cb=None):
        """Core harvest logic shared by JSON and SSE endpoints."""
        print(f"[harvest] Fetching {len(urls)} Reddit URLs (parallel)...")
        posts = fetch_all_parallel(urls, progress_cb)

        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        raw_path = DATA_DIR / f"raw-{ts}.json"
        raw_path.write_text(json.dumps(posts, indent=2))
        print(f"[harvest] Raw saved to {raw_path}")

        if progress_cb:
            progress_cb("analyze", 0, 1)

        prompt = PROMPT_FILE.read_text()
        content_text = format_content_for_prompt(posts)
        full_prompt = prompt + "\n" + content_text

        print(f"[harvest] Running Claude analysis...")
        analysis = run_claude(full_prompt)

        digest_html = build_digest_html(analysis, posts, len(urls), ts)
        digest_filename = f"digest-{ts}.html"
        digest_path = DATA_DIR / digest_filename
        digest_path.write_text(digest_html)
        print(f"[harvest] Digest saved to {digest_path}")

        # Append to knowledge base
        merged_posts = merge_analysis_with_content(analysis, posts)
        _add_to_kb(merged_posts, ts)

        return {
            "digest_url": f"http://localhost:{PORT}/digest/{digest_filename}",
            "post_count": len(urls),
            "fetched": len([p for p in posts if "error" not in p]),
            "errors": len([p for p in posts if "error" in p]),
        }

    def _handle_harvest_json(self):
        """Original JSON endpoint — returns full result at the end."""
        body = self._read_body()
        urls = body.get("urls", [])
        if not urls:
            self._send_json({"error": "no urls provided"}, 400)
            return
        result = self._run_harvest(urls)
        self._send_json(result)

    def _handle_harvest_sse(self):
        """SSE endpoint — streams progress events, then final result."""
        body = self._read_body()
        urls = body.get("urls", [])
        if not urls:
            self._send_json({"error": "no urls provided"}, 400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        self._send_sse("start", {"total": len(urls)})

        def progress(stage, done, total):
            self._send_sse("progress", {"stage": stage, "done": done, "total": total})

        result = self._run_harvest(urls, progress_cb=progress)
        self._send_sse("done", result)

    def _handle_knowledge_dismiss(self):
        """Remove a post from the knowledge base by URL."""
        body = self._read_body()
        url = body.get("url", "")
        if not url:
            self._send_json({"error": "no url provided"}, 400)
            return
        kb = _load_kb()
        if url in kb["posts"]:
            del kb["posts"][url]
            _save_kb(kb)
            print(f"[kb] Dismissed: {url[:80]}")
            self._send_json({"ok": True, "remaining": len(kb["posts"])})
        else:
            self._send_json({"error": "post not found"}, 404)

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {format % args}")


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    server = HTTPServer(("127.0.0.1", PORT), HarvestHandler)
    print(f"Tab Harvester server running on http://localhost:{PORT}")
    print(f"Data dir: {DATA_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
