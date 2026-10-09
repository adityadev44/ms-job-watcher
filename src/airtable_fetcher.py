"""Verified official airtable hiring inventory; shared policy filters downstream."""
from gcc_public_boards import Greenhouse, RateLimitError

_board = Greenhouse("airtable")
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
