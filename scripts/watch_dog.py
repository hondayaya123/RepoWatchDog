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
import re
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
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_LLM_TECH_STACK = "general software development"
DEFAULT_LLM_FOCUS_AREAS = "breaking changes, new features, performance improvements, security vulnerabilities"

# Keywords that mark an item as a critical change (case-insensitive substring match)
_BREAKING_KEYWORDS: dict[str, int] = {
    "security": 3,
    "cve": 3,
    "breaking change": 2,
    "breaking-change": 2,
    "breaking": 2,
    "remove": 1,
    "removed": 1,
    "deprecate": 1,
    "deprecated": 1,
    "migration": 1,
    "priority/high": 1,
}

MAX_COMPACT_ITEMS = 10

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
# Compact summary – rule-based critical change detection (no LLM required)
# ---------------------------------------------------------------------------


def _is_major_version_bump(tag: str) -> bool:
    """Return True when *tag* looks like a major-version release (vX.0.0 / X.0.0)."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag)
    if m:
        major, minor, patch_ver = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return major > 0 and minor == 0 and patch_ver == 0
    return False


def _compute_severity(title: str, labels: list[str]) -> int:
    """Return highest severity score from keyword matching on title + labels."""
    text = (title + " " + " ".join(labels)).lower()
    score = 0
    for keyword, sev in _BREAKING_KEYWORDS.items():
        if keyword in text:
            score = max(score, sev)
    return score


def _item_severity(item: dict, item_type: str) -> int:
    """Return the severity score for a release, PR, or issue dict."""
    title = item.get("title") or item.get("name") or item.get("tag_name") or ""
    labels = [lbl["name"] for lbl in item.get("labels", [])]
    score = _compute_severity(title, labels)
    if item_type == "release":
        tag = item.get("tag_name", "")
        if _is_major_version_bump(tag):
            score = max(score, 2)
        # Also scan first 500 chars of release body
        body = (item.get("body") or "")[:500]
        score = max(score, _compute_severity(body, []))
    return score


def filter_critical_changes(
    releases: list[dict],
    prs: list[dict],
    issues: list[dict],
    max_items: int = MAX_COMPACT_ITEMS,
) -> list[dict]:
    """Return up to *max_items* critical changes, sorted by severity (highest first).

    Each returned dict has keys: ``type`` (str), ``severity`` (int), ``item`` (dict).
    """
    candidates: list[dict] = []

    for r in releases:
        sev = _item_severity(r, "release")
        if sev > 0:
            candidates.append({"type": "release", "severity": sev, "item": r})

    for pr in prs:
        sev = _item_severity(pr, "pr")
        if sev > 0:
            candidates.append({"type": "pr", "severity": sev, "item": pr})

    for issue in issues:
        sev = _item_severity(issue, "issue")
        if sev > 0:
            candidates.append({"type": "issue", "severity": sev, "item": issue})

    candidates.sort(key=lambda x: x["severity"], reverse=True)
    return candidates[:max_items]


def _get_impact_and_action(item: dict, item_type: str, severity: int) -> tuple[str, str]:
    """Return (影響, 我需要做) using simple keyword rules – no LLM needed."""
    title = item.get("title") or item.get("name") or item.get("tag_name") or ""
    labels = [lbl["name"] for lbl in item.get("labels", [])]
    text = (title + " " + " ".join(labels)).lower()

    if "security" in text or "cve" in text:
        return "存在安全漏洞，可能影響系統安全性", "立即更新至最新版本，並確認系統是否受到影響"
    if "breaking change" in text or "breaking-change" in text or "breaking" in text:
        return "API 或行為有破壞性變更，可能導致現有程式碼失效", "閱讀 migration guide 並在測試環境驗證後再升級"
    if "deprecate" in text or "deprecated" in text:
        return "功能已標記為廢棄，未來版本將移除", "規劃遷移至官方推薦的替代方案"
    if "remove" in text or "removed" in text:
        return "功能或 API 已被移除，繼續使用將導致錯誤", "立即更新程式碼以移除對此功能的依賴"
    if "migration" in text:
        return "需要執行資料或配置遷移", "依照 migration guide 完成遷移步驟"
    if item_type == "release" and _is_major_version_bump(item.get("tag_name", "")):
        return "主要版本升級，可能含有破壞性變更", "閱讀 CHANGELOG，評估影響後安排升級計畫"
    if "priority/high" in text:
        return "高優先度問題，可能影響系統穩定性", "評估影響範圍並安排修復"
    return "有顯著變更，需評估對專案的影響", "閱讀詳細說明，確認是否需要採取行動"


def _generate_risks(
    critical: list[dict],
    all_releases: list[dict],
) -> list[str]:
    """Return up to 3 rule-based risk statements in Traditional Chinese."""
    risks: list[str] = []
    has_security = any(c["severity"] >= 3 for c in critical)
    has_breaking = any(c["severity"] == 2 for c in critical)
    has_deprecation = any(
        "deprecat" in ((c["item"].get("title") or c["item"].get("name") or "")).lower()
        for c in critical
    )

    if has_security:
        risks.append("存在安全漏洞，若未及時修補可能導致系統遭受攻擊或資料外洩")
    if has_breaking:
        risks.append("破壞性變更可能使現有整合失效，升級前請在測試環境充分驗證")
    if has_deprecation:
        risks.append("部分功能已廢棄，建議在下次重構時一併完成遷移，避免未來被強制升級")
    if len(all_releases) > 3:
        risks.append(f"本週共有 {len(all_releases)} 個新版本發布，建議評估統一升級的時機以降低維護成本")
    if not risks:
        risks.append("本週無明顯高風險項目，建議持續追蹤後續更新")
    return risks[:3]


def _generate_actions(
    critical: list[dict],
    all_releases: list[dict],
    all_prs: list[dict],
    all_issues: list[dict],
) -> list[str]:
    """Return exactly 3 actionable checklist items in Traditional Chinese."""
    actions: list[str] = []
    has_security = any(c["severity"] >= 3 for c in critical)
    has_breaking = any(c["severity"] == 2 for c in critical)

    if has_security:
        actions.append("優先安裝含安全修補的最新版本，並確認系統未受已知漏洞影響")
    if has_breaking:
        actions.append("閱讀破壞性變更的 migration guide，並於測試環境完成驗證後再上線")
    if critical:
        actions.append("在 GitHub 上訂閱上述重大變更的通知，追蹤後續修補進展")
    if len(actions) < 3:
        actions.append("定期檢視相依套件版本，保持在安全且受支援的版本範圍內")
    if len(actions) < 3:
        actions.append("若本週無重大影響，可規劃於下次維護窗口統一升級非緊急依賴")
    return actions[:3]


def build_compact_report(
    watch_repos: list[dict],
    token: str,
    since: datetime,
    important_labels: list[str] | None = None,
) -> str:
    """Build a compact Traditional-Chinese report highlighting only critical changes.

    No LLM or paid API is required – all logic is rule-based.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    since_str = since.strftime("%Y-%m-%d %H:%M UTC")

    all_releases: list[dict] = []
    all_prs: list[dict] = []
    all_issues: list[dict] = []

    for entry in watch_repos:
        owner = entry["owner"]
        repo = entry["repo"]
        try:
            prs = fetch_merged_prs(owner, repo, token, since)
            releases = fetch_releases(owner, repo, token, since)
            issues = fetch_issues(owner, repo, token, since, important_labels)
            repo_label = f"{owner}/{repo}"
            for x in prs:
                x["_repo"] = repo_label
            for x in releases:
                x["_repo"] = repo_label
            for x in issues:
                x["_repo"] = repo_label
            all_prs.extend(prs)
            all_releases.extend(releases)
            all_issues.extend(issues)
        except requests.HTTPError as exc:
            print(f"⚠️ Failed to fetch {owner}/{repo}: {exc}", file=sys.stderr)

    critical = filter_critical_changes(all_releases, all_prs, all_issues)
    total_changes = len(all_releases) + len(all_prs) + len(all_issues)
    repos_str = "、".join(f"{e['owner']}/{e['repo']}" for e in watch_repos)

    # One-line summary
    if not critical:
        summary = f"本週監測 {repos_str}，共 {total_changes} 項更新，**無發現重大變更**。"
    else:
        sev3 = sum(1 for c in critical if c["severity"] >= 3)
        sev2 = sum(1 for c in critical if c["severity"] == 2)
        rest = len(critical) - sev3 - sev2
        parts: list[str] = []
        if sev3:
            parts.append(f"{sev3} 項安全性問題")
        if sev2:
            parts.append(f"{sev2} 項破壞性變更")
        if rest:
            parts.append(f"{rest} 項重要更新")
        changes_summary = "、".join(parts)
        summary = f"本週監測 {repos_str}，共 {total_changes} 項更新，發現 {changes_summary}，請優先處理。"

    sections: list[str] = [
        "# 📦 RepoWatchDog 週報摘要",
        "",
        f"**報告時間：** {now_str}",
        f"**涵蓋期間：** {since_str} → {now_str}",
        "",
        summary,
        "",
    ]

    # ── 🔥 重大變更 ──────────────────────────────────────────────────────────
    sections.append("## 🔥 重大變更")
    sections.append("")

    if not critical:
        sections.append("_本週無重大變更。_")
        sections.append("")
    else:
        for idx, entry in enumerate(critical, 1):
            item = entry["item"]
            item_type = entry["type"]
            severity = entry["severity"]

            title = item.get("title") or item.get("name") or item.get("tag_name") or ""
            url = item.get("html_url", "")
            repo_label = item.get("_repo", "")

            if severity >= 3:
                badge = "🔴 安全"
            elif severity == 2:
                badge = "🟠 破壞性"
            else:
                badge = "🟡 重要"

            impact, action = _get_impact_and_action(item, item_type, severity)

            sections.append(f"### {idx}. [{badge}] {title}")
            sections.append("")
            sections.append(f"- **影響：** {impact}")
            sections.append(f"- **我需要做：** {action}")
            sections.append(f"- **參考連結：** [{repo_label}]({url})")
            sections.append("")

    # ── 🛡️ 風險與注意事項 ────────────────────────────────────────────────────
    sections.append("## 🛡️ 風險與注意事項")
    sections.append("")
    for i, risk in enumerate(_generate_risks(critical, all_releases), 1):
        sections.append(f"{i}. {risk}")
    sections.append("")

    # ── ✅ 建議行動 checklist ─────────────────────────────────────────────────
    sections.append("## ✅ 建議行動")
    sections.append("")
    for action_item in _generate_actions(critical, all_releases, all_prs, all_issues):
        sections.append(f"- [ ] {action_item}")
    sections.append("")

    sections.append("---")
    sections.append("")
    sections.append(
        "_Generated by [RepoWatchDog](https://github.com/hondayaya123/RepoWatchDog)_ 🐶"
    )

    return "\n".join(sections)

_LLM_PROMPT_TEMPLATE = """\
# Role
你是一位資深的軟體技術分析師，擅長將複雜的 GitHub 技術文件簡化為易於理解的商業與技術決策摘要。

# Task
請分析以下來自 GitHub 的 Release Note 與 Issue 討論，並針對我的需求進行篩選與彙總。

# My Context (我的背景與關注點)
- 我主要關注的技術棧：{tech_stack}
- 我在意的事：{focus_areas}
- 閱讀偏好：請避開艱澀的程式碼細節，用直白的話解釋這些變更對我的專案或開發流程有什麼實質影響。

# Output Requirements
請按以下結構輸出：
1. 🔥 重大變更 (必須注意)：列出會導致程式出錯或需要大幅改動的部分。
2. ✨ 重點新功能：挑選 2-3 個最具代表性的功能，並說明用途。
3. 🛠️ 效能與修復：簡述是否有顯著的優化。
4. 💡 專家建議：根據這些變更，我現在應該「立刻更新」、「再等等」還是「手動調整某個設定」？

# Input Data
{raw_content}\
"""


def _build_llm_prompt(raw_content: str, tech_stack: str, focus_areas: str) -> str:
    """Return the filled-in LLM prompt for a single repository's raw data."""
    return _LLM_PROMPT_TEMPLATE.format(
        tech_stack=tech_stack,
        focus_areas=focus_areas,
        raw_content=raw_content,
    )


def summarize_with_llm(raw_content: str, llm_config: dict, api_key: str) -> str:
    """Call the OpenAI Chat Completions API and return the LLM-generated summary.

    Falls back to *raw_content* unchanged if the API call fails, so that a
    transient error does not prevent the whole report from being published.
    """
    model = llm_config.get("model", DEFAULT_LLM_MODEL)
    tech_stack = llm_config.get("tech_stack", DEFAULT_LLM_TECH_STACK)
    focus_areas = llm_config.get("focus_areas", DEFAULT_LLM_FOCUS_AREAS)
    prompt = _build_llm_prompt(raw_content, tech_stack, focus_areas)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    try:
        resp = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError) as exc:
        print(f"⚠️  LLM summarization failed ({exc}); falling back to raw report.", file=sys.stderr)
        return raw_content


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def build_report(
    watch_repos: list[dict],
    token: str,
    since: datetime,
    important_labels: list[str] | None = None,
    ai_summary: bool = False,
    llm_config: dict | None = None,
    llm_api_key: str = "",
) -> str:
    """Fetch data for every watched repo and return a markdown report.

    When *llm_api_key* is provided and *llm_config* is not None, the releases
    and issues section for each repository is summarised by an LLM instead of
    being rendered as raw markdown.
    """
    use_llm = bool(llm_api_key and llm_config is not None)
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

        # --- Releases + Issues (optionally LLM-summarized) ---
        raw_lines: list[str] = []

        # --- Releases ---
        raw_lines.append(f"### 🚀 New Releases ({len(releases)})")
        raw_lines.append(f"")
        if releases:
            for r in releases:
                tag = r.get("tag_name", "")
                name = r.get("name") or tag
                html_url = r.get("html_url", "")
                pub = r.get("published_at", "")[:10]
                body = (r.get("body") or "").strip()
                raw_lines.append(f"#### [{name}]({html_url}) `{tag}` – {pub}")
                if body:
                    # Indent body as a blockquote (trim to 1000 chars to keep issue readable)
                    trimmed = body[:MAX_RELEASE_BODY_LENGTH] + ("…" if len(body) > MAX_RELEASE_BODY_LENGTH else "")
                    for line in trimmed.splitlines():
                        raw_lines.append(f"> {line}")
                raw_lines.append(f"")
        else:
            raw_lines.append(f"_No new releases this week._")
            raw_lines.append(f"")

        # --- Issues ---
        raw_lines.append(f"### 🐛 重要 Issues ({len(issues)})")
        raw_lines.append(f"")
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
                raw_lines.append(
                    f"- {state_emoji} [#{num} {title}]({html_url}){label_str} – {created}"
                )
            raw_lines.append(f"")
        else:
            raw_lines.append(f"_No new issues this week._")
            raw_lines.append(f"")

        if use_llm:
            print(f"🤖 Summarising {full_name} with LLM ...")
            sections.append(summarize_with_llm("\n".join(raw_lines), llm_config, llm_api_key))  # type: ignore[arg-type]
            sections.append(f"")
        else:
            sections.extend(raw_lines)

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
    llm_config: dict | None = config.get("llm") or None
    # compact_summary: True (default) → use rule-based compact report in Traditional Chinese
    # compact_summary: False → use the full verbose report (original behaviour)
    compact_summary: bool = bool(config.get("compact_summary", True))

    # Allow env var to override lookback_days (used by workflow_dispatch)
    if os.environ.get("LOOKBACK_DAYS"):
        lookback_days = int(os.environ["LOOKBACK_DAYS"])

    # Override report repo from env if provided
    report_owner = os.environ.get("REPORT_OWNER") or report_repo_cfg.get("owner", "")
    report_repo_name = os.environ.get("REPORT_REPO") or report_repo_cfg.get("repo", "")

    if not watch_repos:
        print("No watch_repos configured. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Detect whether this run was triggered manually (workflow_dispatch) or by
    # the scheduler.  GITHUB_EVENT_NAME is set automatically by GitHub Actions;
    # when running locally it defaults to "schedule" so existing behaviour is
    # preserved.
    is_manual = os.environ.get("GITHUB_EVENT_NAME", "schedule") == "workflow_dispatch"

    # Determine look-back window
    now = datetime.now(timezone.utc)
    if is_manual:
        # Manual runs always look back exactly <lookback_days> days from now,
        # ignoring whatever timestamp is stored in the state file.
        since = now - timedelta(days=lookback_days)
        print(
            f"⚡ Manual trigger detected – ignoring state file, "
            f"looking back {lookback_days} day(s)."
        )
    else:
        since = load_state(state_path)
        # Clamp to configured max lookback
        earliest = now - timedelta(days=lookback_days)
        if since < earliest:
            since = earliest

    print(f"Fetching activity since {since.isoformat()} ...")

    llm_api_key = os.environ.get("OPENAI_API_KEY", "")

    if compact_summary:
        print("📋 精簡摘要模式（compact_summary=true）：僅列重大變更，無需 LLM。")
        report_body = build_compact_report(
            watch_repos, token, since,
            important_labels=important_labels or None,
        )
    else:
        if llm_api_key and llm_config is not None:
            print(f"🤖 LLM summarization enabled (model: {llm_config.get('model', DEFAULT_LLM_MODEL)})")
        else:
            print("ℹ️  LLM summarization disabled – set OPENAI_API_KEY and configure 'llm' in config.json to enable.")
        report_body = build_report(
            watch_repos, token, since,
            important_labels=important_labels or None,
            ai_summary=ai_summary_enabled,
            llm_config=llm_config,
            llm_api_key=llm_api_key,
        )

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

    if is_manual:
        print("⚡ Manual trigger – state file NOT updated (last_check.json preserved).")
    else:
        save_state(state_path, now)
        print("State updated.")


if __name__ == "__main__":
    main()
