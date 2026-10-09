"""Zendesk's employer-linked Workday board."""
from gcc_public_boards import Workday, RateLimitError
_board = Workday('zendesk.wd1.myworkdayjobs.com', 'zendesk', 'zendesk')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
