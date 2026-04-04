# 🐶 RepoWatchDog

RepoWatchDog is a lightweight GitHub Actions-powered watchdog that **automatically monitors GitHub repositories weekly** and delivers a formatted summary report as a GitHub Issue.

It tracks:
- 🤖 **AI Summary** – a concise Traditional-Chinese summary of the week's most important changes (powered by GitHub Models / gpt-4o-mini)
- 🔀 **Merged PRs** – pull requests merged this week
- 🚀 **New Releases** – what changed, release notes included
- 🐛 **Important Issues** – newly opened issues filtered by configurable labels
- 📊 **Commit Activity** – total commit count and top-3 contributors

> Default target repo: [`github/copilot-sdk`](https://github.com/github/copilot-sdk) (GitHub Copilot SDK) — fully configurable.

---

## How It Works

1. A **scheduled GitHub Actions workflow** runs every Monday at 09:00 UTC.
2. The Python script (`scripts/watch_dog.py`) queries the GitHub API for new releases, merged PRs, issues, and commit activity since the last check.
3. An **AI summary** is generated (via GitHub Models) describing the week's key changes.
4. A **Markdown summary report** is generated.
5. The report is posted as a **GitHub Issue** (labelled `weekly-report`) in this repository.
6. The last-check timestamp is committed back to `state/last_check.json` so the next run only covers the new period.

---

## Setup

### 1. Fork / clone this repository

```bash
git clone https://github.com/hondayaya123/RepoWatchDog.git
cd RepoWatchDog
```

### 2. Configure which repos to watch

Edit `config.json`:

```json
{
  "watch_repos": [
    {
      "owner": "github",
      "repo": "copilot-sdk",
      "description": "GitHub Copilot SDK"
    }
  ],
  "report_repo": {
    "owner": "",
    "repo": ""
  },
  "lookback_days": 7,
  "important_labels": ["bug", "enhancement", "breaking change", "priority/high"],
  "ai_summary": true
}
```

- `watch_repos` – list of repos to monitor (add as many as you like)
- `report_repo` – where to create the summary issue (leave empty to use the current repo via `REPORT_OWNER` / `REPORT_REPO` env vars)
- `lookback_days` – maximum look-back window (default: 7)
- `important_labels` – only issues with at least one of these labels appear in the report; set to `[]` to show all issues
- `ai_summary` – set to `true` to prepend an AI-generated Traditional-Chinese summary (requires `GITHUB_TOKEN` with GitHub Models access)

### 3. Enable GitHub Actions

The workflow `.github/workflows/weekly_report.yml` uses the built-in `GITHUB_TOKEN`, so **no extra secrets are needed** for basic usage.

Make sure Issues are enabled on your repository (Settings → Features → Issues ✅).

### 4. (Optional) Run manually

Go to **Actions → Weekly Repo Summary Report → Run workflow** and choose how many days to look back.

---

## Local Development

```bash
pip install -r requirements.txt

# Set your token
export GITHUB_TOKEN=ghp_...
export REPORT_OWNER=your-github-username
export REPORT_REPO=RepoWatchDog

python scripts/watch_dog.py
```

### Run tests

```bash
pip install pytest
pytest tests/
```

---

## Project Structure

```
RepoWatchDog/
├── .github/
│   └── workflows/
│       └── weekly_report.yml   # Scheduled workflow
├── scripts/
│   └── watch_dog.py            # Core logic
├── state/
│   └── last_check.json         # Persisted last-check timestamp
├── tests/
│   └── test_watch_dog.py       # Unit tests
├── config.json                 # Watched repos configuration
├── requirements.txt
└── README.md
```

---

## New Features

### 🔀 Merged PRs Tracking

The report now includes a dedicated **Merged PRs** section per repository, showing PR number, title, link, merge date, and author.

### 🐛 Issue Importance Filtering

Set `important_labels` in `config.json` to only surface issues that matter. For example:

```json
"important_labels": ["bug", "enhancement", "breaking change", "priority/high"]
```

Set to `[]` to show all issues (original behaviour).

### 🤖 AI Summary (GitHub Models)

When `"ai_summary": true`, the script calls the [GitHub Models](https://github.com/marketplace/models) API (`gpt-4o-mini` via `https://models.inference.ai.azure.com`) and prepends a Traditional-Chinese summary (≤ 150 characters) describing the week's most important technical changes. The same `GITHUB_TOKEN` is used as the API key. If the call fails the report is still generated normally.

### 📊 Commit Activity Statistics

Each repo section now includes a **開發活躍度** block showing:
- Total commit count for the week
- Top-3 contributors (name + commit count)

---

## Example Report

```markdown
# 📦 RepoWatchDog Weekly Summary

**Report generated:** 2024-06-17 09:00 UTC
**Period covered:** 2024-06-10 09:00 UTC → 2024-06-17 09:00 UTC

## 🤖 AI 本週摘要
本週 copilot-sdk 最重要的變化是新版本 v1.2.0 的發布，並修復了多項關鍵問題。

---

## 🔍 github/copilot-sdk

### 🔀 Merged PRs (5)
- [#927 ephemeral events 改版](https://github.com/...) – 2024-06-12 by @user1

### 🚀 New Releases (1)
#### [v1.2.0](https://github.com/...) `v1.2.0` – 2024-06-12
> - Added new feature
> - Fixed a bug

### 🐛 重要 Issues (3)
- 🟢 [#99 Support for new model](https://github.com/...) [`enhancement`] – 2024-06-11
- 🟢 [#100 Crash on startup](https://github.com/...) [`bug`] – 2024-06-13

### 📊 開發活躍度
- 本週 commits：23
- 前三貢獻者：user1(10), user2(8), user3(5)
```

