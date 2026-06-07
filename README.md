# ms-job-watcher

A lightweight Python tool that monitors job boards for new postings matching your keywords and locations, then sends you notifications via email or Telegram.

## Quick start

```bash
# 1. Create and activate the virtual environment
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Mac / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Edit config.yaml with your search keywords and notification settings

# 4. Run the tests
pytest
```

## Project layout

```
ms-job-watcher/
├── src/            # Main application code lives here
├── tests/          # Automated tests
├── config.yaml     # Your personal settings (not committed to git)
├── requirements.txt
└── README.md
```
