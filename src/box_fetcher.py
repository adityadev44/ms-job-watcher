"""Verified official box hiring inventory; shared policy filters downstream."""
from gcc_public_boards import Greenhouse, RateLimitError

_board = Greenhouse("boxinc")
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
