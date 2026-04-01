# 🐶 RepoWatchDog

RepoWatchDog is a lightweight GitHub Actions-powered watchdog that **automatically monitors GitHub repositories weekly** and delivers a formatted summary report as a GitHub Issue.

It tracks:
- 🚀 **New Releases** – what changed, release notes included
- 🐛 **New Issues** – newly opened issues with labels and state

> Default target repo: [`github/copilot-sdk`](https://github.com/github/copilot-sdk) (GitHub Copilot SDK) — fully configurable.

---

## How It Works

1. A **scheduled GitHub Actions workflow** runs every Monday at 09:00 UTC.
2. The Python script (`scripts/watch_dog.py`) queries the GitHub API for new releases and issues since the last check.
3. A **Markdown summary report** is generated.
4. The report is posted as a **GitHub Issue** (labelled `weekly-report`) in this repository.
5. The last-check timestamp is committed back to `state/last_check.json` so the next run only covers the new period.

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
  "lookback_days": 7
}
```

- `watch_repos` – list of repos to monitor (add as many as you like)
- `report_repo` – where to create the summary issue (leave empty to use the current repo via `REPORT_OWNER` / `REPORT_REPO` env vars)
- `lookback_days` – maximum look-back window (default: 7)

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

## Example Report

```markdown
# 📦 RepoWatchDog Weekly Summary

**Report generated:** 2024-06-17 09:00 UTC
**Period covered:** 2024-06-10 09:00 UTC → 2024-06-17 09:00 UTC

---

## 🔍 github/copilot-sdk

### 🚀 New Releases (1)

#### [v1.2.0](https://github.com/...) `v1.2.0` – 2024-06-12
> - Added new feature
> - Fixed a bug

### 🐛 New Issues (3)

- 🟢 [#99 Support for new model](https://github.com/...) [`enhancement`] – 2024-06-11
- 🟢 [#100 Crash on startup](https://github.com/...) [`bug`] – 2024-06-13
- 🔴 [#98 Documentation unclear](https://github.com/...) [`docs`] – 2024-06-10
```
