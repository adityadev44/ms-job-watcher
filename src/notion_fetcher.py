"""Verified official notion hiring inventory; shared policy filters downstream."""
from collaboration_saas_boards import Ashby, RateLimitError

_board = Ashby("notion")
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
