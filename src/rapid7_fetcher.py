"""Rapid7's verified Workday employer board (tenant name mymoose)."""
from gcc_public_boards import Workday, RateLimitError
_board = Workday('mymoose.wd1.myworkdayjobs.com', 'mymoose', 'careers')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
