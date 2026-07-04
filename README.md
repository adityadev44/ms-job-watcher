# ms-job-watcher

A Python job-board monitor for 74 companies. It searches every 30 minutes for India-based .NET/C# software-engineering roles, removes previously alerted jobs, and sends new matches through Telegram and Gmail.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Validate all registry/config/fetcher wiring without network requests
python src/run_all.py --validate

# Run tests
pytest

# Run one company or the bounded all-company launcher
python src/run_company.py microsoft
python src/run_all.py --workers 10
```

Notification credentials are read from environment variables or `.env`; copy `.env.example` for the supported names. `config.yaml` is tracked and contains operational search policy, not credentials.

## Structure

```text
src/company_registry.py   company inventory and fetch capabilities
src/run_company.py        shared single-company pipeline
src/run_all.py            bounded concurrent launcher
src/matcher.py            location, title, skill, and known-ID filtering
src/notifier.py           Telegram/Gmail delivery and failure tracking
src/*_fetcher.py          company/ATS-specific adapters
config.yaml               shared defaults and company overrides
seen_jobs*.json           append-only alert deduplication state
PLAYBOOK.md               maintenance guide and ATS-specific lessons
```

See [PLAYBOOK.md](PLAYBOOK.md) before adding or changing a company adapter.
