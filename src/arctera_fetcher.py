"""Arctera-identified roles only on its acquiring parent's official ATS."""
from gcc_public_boards import Workday, RateLimitError
_board = Workday('tibco.wd5.myworkdayjobs.com', 'tibco', 'Cloud_Software_Group', employer='Arctera')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
