"""Mythic AI's employer-linked Lever board, not Mythic Entertainment."""
from gcc_public_boards import Lever, RateLimitError
_board = Lever('mythic-ai.com')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
