"""JetBrains' EU Greenhouse board linked by its official jobs-page JavaScript.

The standard public API exposes EU boards too; no invented EU API hostname.
Only country/city-verified India roles are eligible, not European remote jobs.
"""
from gcc_public_boards import Greenhouse, RateLimitError

_board = Greenhouse('jetbrains')
fetch_jobs = _board.fetch_jobs
fetch_job_description = _board.fetch_job_description
