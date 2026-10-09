"""GitHub official careers inventory (separate from Microsoft's hiring feed)."""
from developer_cloud_boards import GitHub, RateLimitError

_board = GitHub()
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
