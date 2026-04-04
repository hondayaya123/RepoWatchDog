"""
RepoWatchDog – weekly watcher for GitHub repository releases and issues.

Usage:
    python scripts/watch_dog.py

Environment variables:
    GITHUB_TOKEN        Personal access token (or GitHub Actions GITHUB_TOKEN)
    REPORT_OWNER        Owner of the repo where the summary issue will be created
    REPORT_REPO         Name of the repo where the summary issue will be created
    CONFIG_PATH         Optional path to config.json (default: config.json next to this script)
    STATE_PATH          Optional path to state/last_check.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RELEASE_BODY_LENGTH = 1000
LABEL_COLOR = "0075ca"  # GitHub's default blue used for informational labels

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"
DEFAULT_STATE_PATH = ROOT_DIR / "state" / "last_check.json"

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(url: str, token: str, params: dict | None = None) -> list | dict:
    """Perform a paginated GET request and return all items."""
    all_items: list = []
    page = 1
    while True:
        p = {"per_page": 100, "page": page, **(params or {})}
        resp = requests.get(url, headers=_headers(token), params=p, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            if not data:
                break
            all_items.extend(data)
            page += 1
        else:
            return data
    return all_items


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def fetch_merged_prs(owner: str, repo: str, token: str, since: datetime) -> list[dict]:
    """Return pull requests merged after *since*."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    prs = _get(url, token, params={"state": "closed", "sort": "updated", "direction": "desc"})
    return [
        pr
        for pr in prs
        if pr.get("merged_at")
        and _parse_dt(pr["merged_at"]) > since
    ]


def fetch_releases(owner: str, repo: str, token: str, since: datetime) -> list[dict]:
    """Return releases published after *since*."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    releases = _get(url, token)
    return [
        r
        for r in releases
        if r.get("published_at")
        and _parse_dt(r["published_at"]) > since
        and not r.get("draft", False)
    ]


def fetch_issues(
    owner: str,
    repo: str,
    token: str,
    since: datetime,
    important_labels: list[str] | None = None,
) -> list[dict]:
    """Return issues (excluding pull requests) created after *since*.

    If *important_labels* is a non-empty list only issues that carry at least
    one matching label are returned.  An empty list or ``None`` returns all issues.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues"
    issues = _get(url, token, params={"state": "all", "since": since.isoformat()})
    result = [
        i
        for i in issues
        if "pull_request" not in i
        and _parse_dt(i.get("created_at", "")) > since
    ]
    if important_labels:
        result = [
            i
            for i in result
            if any(lbl["name"] in important_labels for lbl in i.get("labels", []))
        ]
    return result


def fetch_commit_stats(owner: str, repo: str, token: str, since: datetime) -> dict:
    """Return commit statistics (total count and top-3 contributors) since *since*."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits"
    commits = _get(url, token, params={"since": since.isoformat()})
    total = len(commits)
    author_counts: dict[str, int] = {}
    for c in commits:
        login = (c.get("author") or {}).get("login")
        name = ((c.get("commit") or {}).get("author") or {}).get("name", "unknown")
        author = login or name
        author_counts[author] = author_counts.get(author, 0) + 1
    top3 = sorted(author_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    return {"total": total, "top_contributors": top3}


def generate_ai_summary(
    releases: list[dict],
    prs: list[dict],
    issues: list[dict],
    token: str,
) -> str | None:
    """Generate a Traditional-Chinese summary using GitHub Models (gpt-4o-mini).

    Returns ``None`` if the AI call fails or the openai package is unavailable.
    """
    if OpenAI is None:
        return None
    try:
        client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=token,
        )
        parts: list[str] = []
        if releases:
            tags = ", ".join(r.get("tag_name", "") for r in releases[:5])
            parts.append(f"新版本：{tags}")
        if prs:
            pr_list = "; ".join(f"#{p['number']} {p['title']}" for p in prs[:5])
            parts.append(f"Merged PRs：{pr_list}")
        if issues:
            issue_list = "; ".join(f"#{i['number']} {i['title']}" for i in issues[:5])
            parts.append(f"重要 Issues：{issue_list}")
        if not parts:
            return None
        content = "\n".join(parts)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "請用繁體中文，以150字以內說明本週最重要的技術異動是什麼：\n"
                        + content
                    ),
                }
            ],
            max_tokens=300,
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception:
        return None


def _parse_dt(dt_str: str) -> datetime:
    """Parse ISO-8601 datetime string to UTC-aware datetime."""
    if not dt_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def build_report(
    watch_repos: list[dict],
    token: str,
    since: datetime,
    important_labels: list[str] | None = None,
    ai_summary: bool = False,
) -> str:
    """Fetch data for every watched repo and return a markdown report."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    since_str = since.strftime("%Y-%m-%d %H:%M UTC")

    # -----------------------------------------------------------------------
    # Step 1: collect data for all repos up front (needed for AI summary)
    # -----------------------------------------------------------------------
    all_releases: list[dict] = []
    all_prs: list[dict] = []
    all_issues: list[dict] = []
    repo_data: dict = {}

    for entry in watch_repos:
        owner = entry["owner"]
        repo = entry["repo"]
        key = (owner, repo)
        try:
            prs = fetch_merged_prs(owner, repo, token, since)
            releases = fetch_releases(owner, repo, token, since)
            issues = fetch_issues(owner, repo, token, since, important_labels)
            commits = fetch_commit_stats(owner, repo, token, since)
            repo_data[key] = {
                "prs": prs,
                "releases": releases,
                "issues": issues,
                "commits": commits,
                "error": None,
            }
            all_prs.extend(prs)
            all_releases.extend(releases)
            all_issues.extend(issues)
        except requests.HTTPError as exc:
            repo_data[key] = {"error": str(exc)}

    # -----------------------------------------------------------------------
    # Step 2: build report header
    # -----------------------------------------------------------------------
    sections: list[str] = [
        f"# 📦 RepoWatchDog Weekly Summary",
        f"",
        f"**Report generated:** {now_str}  ",
        f"**Period covered:** {since_str} → {now_str}",
        f"",
    ]

    # -----------------------------------------------------------------------
    # Step 3: AI summary (optional)
    # -----------------------------------------------------------------------
    if ai_summary:
        summary = generate_ai_summary(all_releases, all_prs, all_issues, token)
        if summary:
            sections.append(f"## 🤖 AI 本週摘要")
            sections.append(f"")
            sections.append(summary)
            sections.append(f"")

    # -----------------------------------------------------------------------
    # Step 4: per-repo sections
    # -----------------------------------------------------------------------
    for entry in watch_repos:
        owner = entry["owner"]
        repo = entry["repo"]
        description = entry.get("description", f"{owner}/{repo}")
        full_name = f"{owner}/{repo}"
        repo_url = f"https://github.com/{full_name}"
        key = (owner, repo)

        sections.append(f"---")
        sections.append(f"")
        sections.append(f"## 🔍 [{full_name}]({repo_url})")
        if description:
            sections.append(f"> {description}")
        sections.append(f"")

        data = repo_data.get(key)
        if data is None or data.get("error"):
            err = (data or {}).get("error", "unknown error")
            sections.append(f"⚠️ Failed to fetch data: `{err}`")
            sections.append(f"")
            continue

        prs = data["prs"]
        releases = data["releases"]
        issues = data["issues"]
        commits = data["commits"]

        # --- Merged PRs ---
        sections.append(f"### 🔀 Merged PRs ({len(prs)})")
        sections.append(f"")
        if prs:
            for pr in prs:
                num = pr.get("number")
                title = pr.get("title", "")
                html_url = pr.get("html_url", "")
                merged = (pr.get("merged_at") or "")[:10]
                author = (pr.get("user") or {}).get("login", "unknown")
                sections.append(
                    f"- [#{num} {title}]({html_url}) – {merged} by @{author}"
                )
            sections.append(f"")
        else:
            sections.append(f"_No merged PRs this week._")
            sections.append(f"")

        # --- Releases ---
        sections.append(f"### 🚀 New Releases ({len(releases)})")
        sections.append(f"")
        if releases:
            for r in releases:
                tag = r.get("tag_name", "")
                name = r.get("name") or tag
                html_url = r.get("html_url", "")
                pub = r.get("published_at", "")[:10]
                body = (r.get("body") or "").strip()
                sections.append(f"#### [{name}]({html_url}) `{tag}` – {pub}")
                if body:
                    # Indent body as a blockquote (trim to 1000 chars to keep issue readable)
                    trimmed = body[:MAX_RELEASE_BODY_LENGTH] + ("…" if len(body) > MAX_RELEASE_BODY_LENGTH else "")
                    for line in trimmed.splitlines():
                        sections.append(f"> {line}")
                sections.append(f"")
        else:
            sections.append(f"_No new releases this week._")
            sections.append(f"")

        # --- Issues ---
        sections.append(f"### 🐛 重要 Issues ({len(issues)})")
        sections.append(f"")
        if issues:
            for i in issues:
                num = i.get("number")
                title = i.get("title", "")
                html_url = i.get("html_url", "")
                state = i.get("state", "open")
                created = (i.get("created_at") or "")[:10]
                labels = ", ".join(
                    f"`{lbl['name']}`" for lbl in i.get("labels", [])
                )
                label_str = f" [{labels}]" if labels else ""
                state_emoji = "🟢" if state == "open" else "🔴"
                sections.append(
                    f"- {state_emoji} [#{num} {title}]({html_url}){label_str} – {created}"
                )
            sections.append(f"")
        else:
            sections.append(f"_No new issues this week._")
            sections.append(f"")

        # --- Commit stats ---
        total = commits["total"]
        top3 = commits["top_contributors"]
        sections.append(f"### 📊 開發活躍度")
        sections.append(f"")
        sections.append(f"- 本週 commits：{total}")
        if top3:
            contrib_str = ", ".join(f"{name}({count})" for name, count in top3)
            sections.append(f"- 前三貢獻者：{contrib_str}")
        sections.append(f"")

    sections.append(f"---")
    sections.append(f"")
    sections.append(
        f"_Generated by [RepoWatchDog](https://github.com/hondayaya123/RepoWatchDog)_ 🐶"
    )

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Notification – create a GitHub Issue
# ---------------------------------------------------------------------------


def create_report_issue(
    owner: str, repo: str, token: str, title: str, body: str
) -> str:
    """Create an issue in the report repo and return its URL."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues"
    payload = {"title": title, "body": body, "labels": ["weekly-report"]}
    resp = requests.post(url, headers=_headers(token), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("html_url", "")


def ensure_report_label(owner: str, repo: str, token: str) -> None:
    """Create the 'weekly-report' label if it doesn't exist."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/labels"
    resp = requests.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    existing = {lbl["name"] for lbl in resp.json()}
    if "weekly-report" not in existing:
        requests.post(
            url,
            headers=_headers(token),
            json={"name": "weekly-report", "color": LABEL_COLOR, "description": "Auto-generated weekly summary"},
            timeout=30,
        )


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state(state_path: Path) -> datetime:
    """Return the datetime of the last successful check."""
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            raw = data.get("last_check_utc")
            if raw:
                return _parse_dt(raw)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    # First run: look back 7 days
    return datetime.now(timezone.utc) - timedelta(days=7)


def save_state(state_path: Path, dt: datetime) -> None:
    """Persist the last check timestamp."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"last_check_utc": dt.isoformat()}, indent=2) + "\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    config_path = Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    state_path = Path(os.environ.get("STATE_PATH", DEFAULT_STATE_PATH))

    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(config_path.read_text())
    watch_repos: list[dict] = config.get("watch_repos", [])
    lookback_days: int = int(config.get("lookback_days", 7))
    report_repo_cfg: dict = config.get("report_repo", {})
    important_labels: list[str] = config.get(
        "important_labels", ["bug", "enhancement", "breaking change", "priority/high"]
    )
    ai_summary_enabled: bool = bool(config.get("ai_summary", False))

    # Allow env var to override lookback_days (used by workflow_dispatch)
    if os.environ.get("LOOKBACK_DAYS"):
        lookback_days = int(os.environ["LOOKBACK_DAYS"])

    # Override report repo from env if provided
    report_owner = os.environ.get("REPORT_OWNER") or report_repo_cfg.get("owner", "")
    report_repo_name = os.environ.get("REPORT_REPO") or report_repo_cfg.get("repo", "")

    if not watch_repos:
        print("No watch_repos configured. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Determine look-back window
    since = load_state(state_path)
    # Clamp to configured max lookback
    earliest = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    if since < earliest:
        since = earliest

    print(f"Fetching activity since {since.isoformat()} ...")

    report_body = build_report(
        watch_repos, token, since,
        important_labels=important_labels or None,
        ai_summary=ai_summary_enabled,
    )

    now = datetime.now(timezone.utc)
    week_str = now.strftime("%G-W%V")  # ISO 8601 week date (e.g. 2024-W25)
    issue_title = f"[RepoWatchDog] Weekly Summary {week_str}"

    print("\n" + "=" * 60)
    print(report_body)
    print("=" * 60 + "\n")

    if report_owner and report_repo_name:
        ensure_report_label(report_owner, report_repo_name, token)
        issue_url = create_report_issue(
            report_owner, report_repo_name, token, issue_title, report_body
        )
        print(f"✅ Report issue created: {issue_url}")
    else:
        print(
            "ℹ️  REPORT_OWNER / REPORT_REPO not configured – report printed to stdout only."
        )

    save_state(state_path, now)
    print("State updated.")


if __name__ == "__main__":
    main()
