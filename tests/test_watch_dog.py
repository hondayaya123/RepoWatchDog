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
# build_report
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


def _make_response(data):
    mock = MagicMock()
    mock.json.return_value = data
    mock.raise_for_status = MagicMock()
    return mock


@patch("watch_dog.requests.get")
def test_build_report_with_data(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": "Test repo"}]

    # Two calls per repo: one for releases, pages for issues
    mock_get.side_effect = [
        _make_response([MOCK_RELEASE]),  # releases page 1
        _make_response([]),              # releases page 2 (empty = stop)
        _make_response([MOCK_ISSUE]),    # issues page 1
        _make_response([]),              # issues page 2 (empty = stop)
    ]

    report = build_report(watch_repos, token="fake", since=since)

    assert "RepoWatchDog Weekly Summary" in report
    assert "example/repo" in report
    assert "v1.2.3" in report
    assert "Version 1.2.3" in report
    assert "Something is broken" in report
    assert "#42" in report
    assert "`bug`" in report


@patch("watch_dog.requests.get")
def test_build_report_no_activity(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    mock_get.side_effect = [
        _make_response([]),  # releases – empty
        _make_response([]),  # issues – empty
    ]

    report = build_report(watch_repos, token="fake", since=since)

    assert "No new releases this week" in report
    assert "No new issues this week" in report


@patch("watch_dog.requests.get")
def test_build_report_draft_release_excluded(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    draft_release = {**MOCK_RELEASE, "draft": True}

    mock_get.side_effect = [
        _make_response([draft_release]),
        _make_response([]),
        _make_response([]),
    ]

    report = build_report(watch_repos, token="fake", since=since)
    assert "No new releases this week" in report


@patch("watch_dog.requests.get")
def test_build_report_pr_excluded_from_issues(mock_get):
    since = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    watch_repos = [{"owner": "example", "repo": "repo", "description": ""}]

    pr_item = {**MOCK_ISSUE, "pull_request": {"url": "https://..."}}

    mock_get.side_effect = [
        _make_response([]),          # releases empty
        _make_response([pr_item]),   # issues contains only a PR
        _make_response([]),
    ]

    report = build_report(watch_repos, token="fake", since=since)
    assert "No new issues this week" in report


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
        _make_response([release_a]),  # releases page 1
        _make_response([release_b]),  # releases page 2
        _make_response([]),           # releases page 3 (empty = stop)
        _make_response([]),           # issues empty
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

    def fake_build(watch_repos, token, since):
        captured["since"] = since
        return "# test"

    with patch("watch_dog.build_report", side_effect=fake_build):
        with patch("watch_dog.save_state"):
            wd.main()

    # 'since' should be clamped to ~14 days ago (not 30)
    diff = datetime.now(timezone.utc) - captured["since"]
    assert 13 <= diff.days <= 15
