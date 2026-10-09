"""Graphcore's employer-linked Greenhouse board."""
from gcc_public_boards import Greenhouse, RateLimitError
_board = Greenhouse('graphcore')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
