"""Elastic's Greenhouse inventory; posting URLs resolve to jobs.elastic.co."""
from gcc_public_boards import Greenhouse, RateLimitError

_board = Greenhouse('elastic')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
