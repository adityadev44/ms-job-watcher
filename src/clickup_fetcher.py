"""Verified official clickup hiring inventory; shared policy filters downstream."""
from collaboration_saas_boards import Ashby, RateLimitError

_board = Ashby("clickup")
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
