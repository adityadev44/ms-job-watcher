"""Verified official miro hiring inventory; shared policy filters downstream."""
from collaboration_saas_boards import Miro, RateLimitError

_board = Miro()
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
