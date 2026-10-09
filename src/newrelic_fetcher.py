"""New Relic's Greenhouse board linked from its official careers page."""
from gcc_public_boards import Greenhouse, RateLimitError

_board = Greenhouse('newrelic')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
