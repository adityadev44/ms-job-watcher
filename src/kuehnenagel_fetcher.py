"""Kuehne+Nagel's verified public Phenom India search and full details."""
from gcc_public_boards import Phenom, RateLimitError
_board = Phenom()
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
