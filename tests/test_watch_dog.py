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
    _parse_dt,
    build_report,
    fetch_commit_stats,
    fetch_issues,
    fetch_merged_prs,
    generate_ai_summary,
    load_state,
    save_state,
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
