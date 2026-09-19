# Local primary runner

This setup makes one Windows machine the primary execution environment while keeping the repository as the shared source of code.

## Ownership model

Use one active production runner at a time.

- Primary machine: runs the local watcher and the twice-daily referral digest.
- Secondary/admin machine: keeps repository access but does not run the same schedules.
- GitHub: stores reusable code only. Do not commit credentials, resumes, browser profiles, cookies, referral tables, or generated personal data.

Running the same schedule on two machines can create duplicate emails or race on checkpoint state.

## One-time machine setup

1. Sign into GitHub with the machine owner's own GitHub account.
2. Clone this repository.
3. In PowerShell from the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_windows_local.ps1
```

4. Open the generated `.env` and fill only local values:

```text
GMAIL_USER=
GMAIL_APP_PASSWORD=
ALERT_RECIPIENT=
```

Use a Gmail App Password, not the normal Google password.

5. Smoke-test locally:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_local_watcher.ps1
```

6. After the smoke test succeeds, install the Windows watcher task:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_windows_local.ps1 -InstallWatcherTask
```

## Browser and referral-digest setup

Create a dedicated browser profile on the primary machine and sign into the candidate's own Gmail and LinkedIn accounts. Do not copy browser cookies from another person's machine.

Sign into ChatGPT/Codex on the primary machine and create two scheduled referral-digest runs:

- 2:00 PM Asia/Kolkata
- 7:00 PM Asia/Kolkata

Use the prompt in `docs/REFERRAL_DIGEST_AUTOMATION.md`.

LinkedIn messages and connection requests remain manual. The automation researches contacts and drafts copy-pasteable messages only.

## Cutover

Do not disable the existing GitHub/cloud watcher before the first successful local smoke test.

Once the local watcher and referral digest are both confirmed working:

1. Stop the old machine's duplicate schedules.
2. Disable the GitHub watcher schedule if the local machine is intended to be the sole production watcher.
3. Keep only one primary runner active.
4. Keep GitHub as the shared code/admin layer.

The referral digest uses successful sent mail as its checkpoint. At first cutover, seed it from the last successfully covered digest/job IDs so historical jobs are not resent.
