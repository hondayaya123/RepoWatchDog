"""
Unit tests for watch_dog.py
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make scripts importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from watch_dog import (
    _build_llm_prompt,
    _compute_severity,
    _is_major_version_bump,
    _parse_dt,
    build_compact_report,
    build_report,
    fetch_commit_stats,
    fetch_issues,
    fetch_merged_prs,
    filter_critical_changes,
    generate_ai_summary,
    load_state,
    save_state,
    summarize_with_llm,
)


# ---------------------------------------------------------------------------
# _parse_dt
# ---------------------------------------------------------------------------


def test_parse_dt_utc():
    dt = _parse_dt("2024-01-15T09:00:00Z")
    assert dt == datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)


def test_parse_dt_offset():
    dt = _parse_dt("2024-01-15T09:00:00+00:00")
    assert dt == datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)


def test_parse_dt_empty():
    dt = _parse_dt("")
    assert dt == datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


def test_load_state_no_file(tmp_path):
    state_file = tmp_path / "state" / "last_check.json"
    since = load_state(state_file)
    # Should return something close to 7 days ago
    diff = datetime.now(timezone.utc) - since
    assert 6 <= diff.days <= 8


def test_save_and_load_state(tmp_path):
    state_file = tmp_path / "state" / "last_check.json"
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    save_state(state_file, now)
    loaded = load_state(state_file)
    assert loaded == now


def test_load_state_corrupt_file(tmp_path):
    state_file = tmp_path / "last_check.json"
    state_file.write_text("not json")
    since = load_state(state_file)
    diff = datetime.now(timezone.utc) - since
    assert 6 <= diff.days <= 8


# ---------------------------------------------------------------------------
# Shared mock data
# ---------------------------------------------------------------------------

MOCK_RELEASE = {
    "tag_name": "v1.2.3",
    "name": "Version 1.2.3",
    "html_url": "https://github.com/example/repo/releases/tag/v1.2.3",
    "published_at": "2024-06-10T10:00:00Z",
    "draft": False,
    "body": "- Added new feature\n- Fixed a bug",
}

MOCK_ISSUE = {
    "number": 42,
    "title": "Something is broken",
    "html_url": "https://github.com/example/repo/issues/42",
    "state": "open",
    "created_at": "2024-06-11T08:00:00Z",
    "labels": [{"name": "bug"}],
}

MOCK_PR = {
    "number": 10,
    "title": "Add cool feature",
    "html_url": "https://github.com/example/repo/pull/10",
    "merged_at": "2024-06-10T09:00:00Z",
    "user": {"login": "octocat"},
}

MOCK_COMMIT = {
    "sha": "abc123",
    "author": {"login": "octocat"},
    "commit": {"author": {"name": "Octocat"}},
}


def _make_response(data):
    mock = MagicMock()
    mock.json.return_value = data
    mock.raise_for_status = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# fetch_merged_prs
# ---------------------------------------------------------------------------


@patch("watch_dog.requests.get")
def test_fetch_merged_prs_returns_merged(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    mock_get.side_effect = [
        _make_response([MOCK_PR]),
        _make_response([]),
    ]
    result = fetch_merged_prs("example", "repo", "fake", since)
    assert len(result) == 1
    assert result[0]["number"] == 10


@patch("watch_dog.requests.get")
def test_fetch_merged_prs_excludes_unmerged(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    unmerged = {**MOCK_PR, "merged_at": None}
    mock_get.side_effect = [
        _make_response([unmerged]),
        _make_response([]),
    ]
    result = fetch_merged_prs("example", "repo", "fake", since)
    assert result == []


@patch("watch_dog.requests.get")
def test_fetch_merged_prs_excludes_old(mock_get):
    since = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    mock_get.side_effect = [
        _make_response([MOCK_PR]),  # merged_at is 2024-06-10, before since
        _make_response([]),
    ]
    result = fetch_merged_prs("example", "repo", "fake", since)
    assert result == []


# ---------------------------------------------------------------------------
# fetch_issues with important_labels
# ---------------------------------------------------------------------------


@patch("watch_dog.requests.get")
def test_fetch_issues_no_filter(mock_get):
    """important_labels=None returns all issues."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    no_label_issue = {**MOCK_ISSUE, "labels": []}
    mock_get.side_effect = [
        _make_response([MOCK_ISSUE, no_label_issue]),
        _make_response([]),
    ]
    result = fetch_issues("example", "repo", "fake", since, important_labels=None)
    assert len(result) == 2


@patch("watch_dog.requests.get")
def test_fetch_issues_empty_labels_shows_all(mock_get):
    """important_labels=[] returns all issues (empty list = no filter)."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    no_label_issue = {**MOCK_ISSUE, "labels": []}
    mock_get.side_effect = [
        _make_response([MOCK_ISSUE, no_label_issue]),
        _make_response([]),
    ]
    result = fetch_issues("example", "repo", "fake", since, important_labels=[])
    assert len(result) == 2


@patch("watch_dog.requests.get")
def test_fetch_issues_filters_by_label(mock_get):
    """important_labels non-empty filters issues to matching labels only."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    docs_issue = {**MOCK_ISSUE, "number": 99, "labels": [{"name": "docs"}]}
    mock_get.side_effect = [
        _make_response([MOCK_ISSUE, docs_issue]),
        _make_response([]),
    ]
    result = fetch_issues(
        "example", "repo", "fake", since, important_labels=["bug", "enhancement"]
    )
    # Only MOCK_ISSUE (label=bug) should pass
    assert len(result) == 1
    assert result[0]["number"] == 42


# ---------------------------------------------------------------------------
# fetch_commit_stats
# ---------------------------------------------------------------------------


@patch("watch_dog.requests.get")
def test_fetch_commit_stats_total(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    commit_b = {**MOCK_COMMIT, "sha": "def456"}
    mock_get.side_effect = [
        _make_response([MOCK_COMMIT, commit_b]),
        _make_response([]),
    ]
    stats = fetch_commit_stats("example", "repo", "fake", since)
    assert stats["total"] == 2


@patch("watch_dog.requests.get")
def test_fetch_commit_stats_top_contributors(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    commit_other = {
        "sha": "def456",
        "author": {"login": "alice"},
        "commit": {"author": {"name": "Alice"}},
    }
    mock_get.side_effect = [
        _make_response([MOCK_COMMIT, MOCK_COMMIT, commit_other]),
        _make_response([]),
    ]
    stats = fetch_commit_stats("example", "repo", "fake", since)
    top = dict(stats["top_contributors"])
    assert top["octocat"] == 2
    assert top["alice"] == 1


@patch("watch_dog.requests.get")
def test_fetch_commit_stats_empty(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    mock_get.side_effect = [_make_response([])]
    stats = fetch_commit_stats("example", "repo", "fake", since)
    assert stats["total"] == 0
    assert stats["top_contributors"] == []


# ---------------------------------------------------------------------------
# generate_ai_summary
# ---------------------------------------------------------------------------


def test_generate_ai_summary_success():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "本週最重要的技術異動是新版本發布。"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("watch_dog.OpenAI", return_value=mock_client) as mock_cls:
        result = generate_ai_summary([MOCK_RELEASE], [MOCK_PR], [MOCK_ISSUE], "fake")

    assert result == "本週最重要的技術異動是新版本發布。"
    mock_cls.assert_called_once_with(
        base_url="https://models.inference.ai.azure.com",
        api_key="fake",
    )


def test_generate_ai_summary_api_failure():
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("API error")

    with patch("watch_dog.OpenAI", return_value=mock_client):
        result = generate_ai_summary([MOCK_RELEASE], [MOCK_PR], [MOCK_ISSUE], "fake")

    assert result is None


def test_generate_ai_summary_openai_unavailable():
    with patch("watch_dog.OpenAI", None):
        result = generate_ai_summary([MOCK_RELEASE], [MOCK_PR], [MOCK_ISSUE], "fake")
    assert result is None


def test_generate_ai_summary_no_data():
    """Returns None when there is nothing to summarize."""
    mock_client = MagicMock()
    with patch("watch_dog.OpenAI", return_value=mock_client):
        result = generate_ai_summary([], [], [], "fake")
    assert result is None
    mock_client.chat.completions.create.assert_not_called()


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


@patch("watch_dog.requests.get")
def test_build_report_with_data(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": "Test repo"}]

    mock_get.side_effect = [
        _make_response([MOCK_PR]),      # merged PRs page 1
        _make_response([]),             # merged PRs page 2 (empty = stop)
        _make_response([MOCK_RELEASE]), # releases page 1
        _make_response([]),             # releases page 2 (empty = stop)
        _make_response([MOCK_ISSUE]),   # issues page 1
        _make_response([]),             # issues page 2 (empty = stop)
        _make_response([MOCK_COMMIT]),  # commits page 1
        _make_response([]),             # commits page 2 (empty = stop)
    ]

    report = build_report(watch_repos, token="fake", since=since)

    assert "RepoWatchDog Weekly Summary" in report
    assert "example/repo" in report
    assert "v1.2.3" in report
    assert "Version 1.2.3" in report
    assert "Something is broken" in report
    assert "#42" in report
    assert "`bug`" in report
    assert "Merged PRs" in report
    assert "#10" in report
    assert "octocat" in report
    assert "開發活躍度" in report


@patch("watch_dog.requests.get")
def test_build_report_no_activity(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    mock_get.side_effect = [
        _make_response([]),  # merged PRs – empty
        _make_response([]),  # releases – empty
        _make_response([]),  # issues – empty
        _make_response([]),  # commits – empty
    ]

    report = build_report(watch_repos, token="fake", since=since)

    assert "No merged PRs this week" in report
    assert "No new releases this week" in report
    assert "No new issues this week" in report


@patch("watch_dog.requests.get")
def test_build_report_draft_release_excluded(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    draft_release = {**MOCK_RELEASE, "draft": True}

    mock_get.side_effect = [
        _make_response([]),              # merged PRs – empty
        _make_response([draft_release]), # releases page 1
        _make_response([]),              # releases page 2 (empty)
        _make_response([]),              # issues – empty
        _make_response([]),              # commits – empty
    ]

    report = build_report(watch_repos, token="fake", since=since)
    assert "No new releases this week" in report


@patch("watch_dog.requests.get")
def test_build_report_pr_excluded_from_issues(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    pr_item = {**MOCK_ISSUE, "pull_request": {"url": "https://..."}}

    mock_get.side_effect = [
        _make_response([]),          # merged PRs – empty
        _make_response([]),          # releases – empty
        _make_response([pr_item]),   # issues contains only a PR
        _make_response([]),          # issues page 2 (empty)
        _make_response([]),          # commits – empty
    ]

    report = build_report(watch_repos, token="fake", since=since)
    assert "No new issues this week" in report


@patch("watch_dog.requests.get")
def test_build_report_important_labels_filter(mock_get):
    """Issues without matching labels are excluded from the report."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    docs_issue = {**MOCK_ISSUE, "number": 99, "title": "Docs only", "labels": [{"name": "docs"}]}

    mock_get.side_effect = [
        _make_response([]),                          # merged PRs – empty
        _make_response([]),                          # releases – empty
        _make_response([MOCK_ISSUE, docs_issue]),    # issues: bug + docs
        _make_response([]),                          # issues page 2
        _make_response([]),                          # commits – empty
    ]

    report = build_report(
        watch_repos, token="fake", since=since, important_labels=["bug"]
    )
    assert "Something is broken" in report   # bug label → included
    assert "Docs only" not in report          # docs label → excluded


@patch("watch_dog.requests.get")
def test_build_report_ai_summary_included(mock_get):
    """AI summary section appears at the top when ai_summary=True."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    mock_get.side_effect = [
        _make_response([]),  # merged PRs
        _make_response([]),  # releases
        _make_response([]),  # issues
        _make_response([]),  # commits
    ]

    with patch("watch_dog.generate_ai_summary", return_value="AI 摘要內容"):
        report = build_report(watch_repos, token="fake", since=since, ai_summary=True)

    assert "🤖 AI 本週摘要" in report
    assert "AI 摘要內容" in report


@patch("watch_dog.requests.get")
def test_build_report_ai_summary_skipped_on_failure(mock_get):
    """Report still generates when AI summary fails."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    mock_get.side_effect = [
        _make_response([]),  # merged PRs
        _make_response([]),  # releases
        _make_response([]),  # issues
        _make_response([]),  # commits
    ]

    with patch("watch_dog.generate_ai_summary", return_value=None):
        report = build_report(watch_repos, token="fake", since=since, ai_summary=True)

    assert "RepoWatchDog Weekly Summary" in report
    assert "🤖 AI 本週摘要" not in report


@patch("watch_dog.requests.get")
def test_build_report_commit_stats_section(mock_get):
    """Commit stats are displayed in the report."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    mock_get.side_effect = [
        _make_response([]),             # merged PRs
        _make_response([]),             # releases
        _make_response([]),             # issues
        _make_response([MOCK_COMMIT]),  # commits page 1
        _make_response([]),             # commits page 2
    ]

    report = build_report(watch_repos, token="fake", since=since)
    assert "開發活躍度" in report
    assert "本週 commits：1" in report
    assert "octocat" in report


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@patch("watch_dog.requests.get")
def test_get_paginates_releases(mock_get):
    """_get should collect items across multiple pages."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    release_a = {**MOCK_RELEASE, "tag_name": "v1.0.0", "name": "v1.0.0",
                 "html_url": "https://github.com/example/repo/releases/tag/v1.0.0"}
    release_b = {**MOCK_RELEASE, "tag_name": "v1.1.0", "name": "v1.1.0",
                 "html_url": "https://github.com/example/repo/releases/tag/v1.1.0"}

    mock_get.side_effect = [
        _make_response([]),           # merged PRs – empty
        _make_response([release_a]),  # releases page 1
        _make_response([release_b]),  # releases page 2
        _make_response([]),           # releases page 3 (empty = stop)
        _make_response([]),           # issues – empty
        _make_response([]),           # commits – empty
    ]

    report = build_report(watch_repos, token="fake", since=since)

    assert "v1.0.0" in report
    assert "v1.1.0" in report
    assert "New Releases (2)" in report


# ---------------------------------------------------------------------------
# LOOKBACK_DAYS env override
# ---------------------------------------------------------------------------


def test_lookback_days_env_override(tmp_path, monkeypatch):
    """LOOKBACK_DAYS env var should clamp a stale state to at most lookback_days days ago."""
    import watch_dog as wd

    monkeypatch.setenv("LOOKBACK_DAYS", "14")
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setenv("REPORT_OWNER", "")
    monkeypatch.setenv("REPORT_REPO", "")

    config = {
        "watch_repos": [{"owner": "ex", "repo": "r", "description": ""}],
        "report_repo": {},
        "lookback_days": 7,  # config says 7, env says 14
        "important_labels": [],
        "ai_summary": False,
        "compact_summary": False,  # use full report so build_report is called
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config))

    # Write state that is 30 days old so it would be clamped by lookback_days=14
    state_path = tmp_path / "state" / "last_check.json"
    old_date = datetime.now(timezone.utc) - timedelta(days=30)
    save_state(state_path, old_date)

    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("STATE_PATH", str(state_path))

    captured = {}

    def fake_build(watch_repos, token, since, **kwargs):
        captured["since"] = since
        return "# test"

    with patch("watch_dog.build_report", side_effect=fake_build):
        with patch("watch_dog.save_state"):
            wd.main()

    # 'since' should be clamped to ~14 days ago (not 30)
    diff = datetime.now(timezone.utc) - captured["since"]
    assert 13 <= diff.days <= 15


# ---------------------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------------------


def test_build_llm_prompt_contains_sections():
    prompt = _build_llm_prompt("some raw content", "React, Node.js", "breaking changes")
    assert "🔥" in prompt
    assert "✨" in prompt
    assert "🛠️" in prompt
    assert "💡" in prompt
    assert "React, Node.js" in prompt
    assert "breaking changes" in prompt
    assert "some raw content" in prompt


def _make_llm_response(content: str):
    mock = MagicMock()
    mock.json.return_value = {"choices": [{"message": {"content": content}}]}
    mock.raise_for_status = MagicMock()
    return mock


@patch("watch_dog.requests.post")
def test_summarize_with_llm_success(mock_post):
    mock_post.return_value = _make_llm_response("🔥 重大變更\n✨ 重點新功能")

    llm_config = {"model": "gpt-4o-mini", "tech_stack": "Python", "focus_areas": "security"}
    result = summarize_with_llm("raw data", llm_config, "fake-key")

    assert "🔥 重大變更" in result
    assert "✨ 重點新功能" in result
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "Bearer fake-key" in call_kwargs[1]["headers"]["Authorization"]


@patch("watch_dog.requests.post")
def test_summarize_with_llm_fallback_on_error(mock_post):
    import requests as req
    mock_post.side_effect = req.exceptions.ConnectionError("network error")

    result = summarize_with_llm("raw data", {}, "fake-key")
    assert result == "raw data"


@patch("watch_dog.requests.get")
@patch("watch_dog.requests.post")
def test_build_report_with_llm(mock_post, mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": "Test repo"}]

    mock_get.side_effect = [
        _make_response([MOCK_PR]),      # merged PRs page 1
        _make_response([]),             # merged PRs page 2
        _make_response([MOCK_RELEASE]), # releases page 1
        _make_response([]),             # releases page 2
        _make_response([MOCK_ISSUE]),   # issues page 1
        _make_response([]),             # issues page 2
        _make_response([MOCK_COMMIT]),  # commits page 1
        _make_response([]),             # commits page 2
    ]

    llm_summary = "🔥 重大變更: 無\n✨ 重點新功能: 新增功能\n🛠️ 效能與修復: 修復 bug\n💡 專家建議: 立刻更新"
    mock_post.return_value = _make_llm_response(llm_summary)

    llm_config = {"model": "gpt-4o-mini", "tech_stack": "Python", "focus_areas": "breaking changes"}
    report = build_report(watch_repos, token="fake", since=since, llm_config=llm_config, llm_api_key="fake-key")

    assert "RepoWatchDog Weekly Summary" in report
    assert "example/repo" in report
    assert "🔥 重大變更" in report
    assert "💡 專家建議" in report
    mock_post.assert_called_once()


@patch("watch_dog.requests.get")
def test_build_report_without_llm_key_uses_raw(mock_get):
    """When no LLM key is provided, the raw markdown report is used."""
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": "Test repo"}]

    mock_get.side_effect = [
        _make_response([]),             # merged PRs – empty
        _make_response([MOCK_RELEASE]), # releases page 1
        _make_response([]),             # releases page 2
        _make_response([MOCK_ISSUE]),   # issues page 1
        _make_response([]),             # issues page 2
        _make_response([]),             # commits – empty
    ]

    llm_config = {"model": "gpt-4o-mini"}
    report = build_report(watch_repos, token="fake", since=since, llm_config=llm_config, llm_api_key="")

    # Raw release and issue info should appear
    assert "v1.2.3" in report
    assert "Something is broken" in report


# ---------------------------------------------------------------------------
# _is_major_version_bump
# ---------------------------------------------------------------------------


def test_is_major_version_bump_v2_0_0():
    assert _is_major_version_bump("v2.0.0") is True


def test_is_major_version_bump_no_v_prefix():
    assert _is_major_version_bump("3.0.0") is True


def test_is_major_version_bump_minor_not_zero():
    assert _is_major_version_bump("v2.1.0") is False


def test_is_major_version_bump_patch_not_zero():
    assert _is_major_version_bump("v2.0.1") is False


def test_is_major_version_bump_v0_is_not_major():
    # v0.x.0 is typically pre-release, not a major bump
    assert _is_major_version_bump("v0.1.0") is False


def test_is_major_version_bump_non_semver():
    assert _is_major_version_bump("latest") is False


# ---------------------------------------------------------------------------
# _compute_severity
# ---------------------------------------------------------------------------


def test_compute_severity_security_keyword():
    assert _compute_severity("Security fix for CVE-2024-1234", []) == 3


def test_compute_severity_cve_in_label():
    assert _compute_severity("Some release", ["CVE"]) == 3


def test_compute_severity_breaking_change():
    assert _compute_severity("breaking change in API", []) == 2


def test_compute_severity_deprecate():
    assert _compute_severity("Deprecate old endpoint", []) == 1


def test_compute_severity_no_match():
    assert _compute_severity("Minor documentation update", []) == 0


def test_compute_severity_highest_wins():
    # Both security (3) and breaking (2) present – should return 3
    assert _compute_severity("Security breaking change", []) == 3


# ---------------------------------------------------------------------------
# filter_critical_changes
# ---------------------------------------------------------------------------

MOCK_SECURITY_RELEASE = {
    "tag_name": "v1.2.4",
    "name": "Security Fix",
    "title": "Security patch CVE-2024-9999",
    "html_url": "https://github.com/example/repo/releases/tag/v1.2.4",
    "published_at": "2024-06-12T10:00:00Z",
    "draft": False,
    "body": "Security fix for CVE-2024-9999",
    "labels": [],
}

MOCK_BREAKING_PR = {
    "number": 20,
    "title": "Breaking change: remove old API",
    "html_url": "https://github.com/example/repo/pull/20",
    "merged_at": "2024-06-11T09:00:00Z",
    "user": {"login": "octocat"},
    "labels": [],
}

MOCK_NORMAL_PR = {
    "number": 21,
    "title": "Update README",
    "html_url": "https://github.com/example/repo/pull/21",
    "merged_at": "2024-06-11T10:00:00Z",
    "user": {"login": "octocat"},
    "labels": [],
}


def test_filter_critical_changes_returns_only_critical():
    result = filter_critical_changes(
        releases=[MOCK_SECURITY_RELEASE],
        prs=[MOCK_BREAKING_PR, MOCK_NORMAL_PR],
        issues=[],
    )
    titles = [e["item"].get("name") or e["item"].get("title") for e in result]
    assert "Security Fix" in titles
    assert "Breaking change: remove old API" in titles
    # Normal PR should be excluded
    assert not any("Update README" in (t or "") for t in titles)


def test_filter_critical_changes_sorted_by_severity():
    result = filter_critical_changes(
        releases=[MOCK_SECURITY_RELEASE],
        prs=[MOCK_BREAKING_PR],
        issues=[],
    )
    # Security (3) should come before breaking (2)
    assert result[0]["severity"] >= result[-1]["severity"]
    assert result[0]["severity"] == 3


def test_filter_critical_changes_respects_max_items():
    many_breaking_prs = [
        {**MOCK_BREAKING_PR, "number": i, "title": f"Breaking change #{i}"}
        for i in range(20)
    ]
    result = filter_critical_changes(releases=[], prs=many_breaking_prs, issues=[], max_items=5)
    assert len(result) == 5


def test_filter_critical_changes_major_release_included():
    major_release = {
        "tag_name": "v2.0.0",
        "name": "Version 2.0.0",
        "html_url": "https://github.com/example/repo/releases/tag/v2.0.0",
        "published_at": "2024-06-10T10:00:00Z",
        "draft": False,
        "body": "",
        "labels": [],
    }
    result = filter_critical_changes(releases=[major_release], prs=[], issues=[])
    assert len(result) == 1
    assert result[0]["severity"] == 2


def test_filter_critical_changes_empty_inputs():
    result = filter_critical_changes(releases=[], prs=[], issues=[])
    assert result == []


# ---------------------------------------------------------------------------
# build_compact_report
# ---------------------------------------------------------------------------


@patch("watch_dog.requests.get")
def test_build_compact_report_structure(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": "Test repo"}]

    mock_get.side_effect = [
        _make_response([MOCK_BREAKING_PR]),   # merged PRs page 1
        _make_response([]),                   # merged PRs page 2
        _make_response([MOCK_SECURITY_RELEASE]),  # releases page 1
        _make_response([]),                   # releases page 2
        _make_response([MOCK_ISSUE]),         # issues page 1
        _make_response([]),                   # issues page 2
    ]

    report = build_compact_report(watch_repos, token="fake", since=since)

    assert "# 📦 RepoWatchDog 週報摘要" in report
    assert "## 🔥 重大變更" in report
    assert "## 🛡️ 風險與注意事項" in report
    assert "## ✅ 建議行動" in report
    # At least one action item checkbox
    assert "- [ ]" in report
    # Summary line should be in Traditional Chinese
    assert "本週監測" in report


@patch("watch_dog.requests.get")
def test_build_compact_report_no_critical_changes(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": "Test repo"}]

    mock_get.side_effect = [
        _make_response([MOCK_PR]),     # merged PRs page 1 (normal PR, not critical)
        _make_response([]),            # merged PRs page 2
        _make_response([MOCK_RELEASE]),  # releases page 1 (v1.2.3, not major)
        _make_response([]),            # releases page 2
        _make_response([]),            # issues page 1 (no issues)
        _make_response([]),            # issues page 2
    ]

    report = build_compact_report(watch_repos, token="fake", since=since)

    assert "本週無重大變更" in report
    assert "## 🔥 重大變更" in report

