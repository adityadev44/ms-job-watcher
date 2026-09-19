# Shivangi activation handoff

Status checked on 2026-09-19.

## Current repository state

- Repository owner: adityadev44
- Repository visibility: public
- Shivangi GitHub username: Shikant
- Current Shikant repository permission: read
- Local Windows runner support is already merged into master.
- The existing GitHub watcher/heartbeat schedules remain active until the local machine is proven working.

## Referral digest checkpoint

Cutover review of the previous sender's Sent mail found no successfully sent referral digest matching the scheduled workflow. The scheduled attempts in the prior workflow failed before producing a usable digest.

Therefore the local referral-digest setup should start with no historical successful-digest checkpoint. Do not import ordinary forwarded job emails as completed referral digests.

## Safe activation sequence

1. Pull latest master on the primary Windows machine.
2. Sign into the candidate's own Gmail and LinkedIn sessions locally.
3. Complete the ignored local .env and resume path.
4. Run one controlled watcher smoke test.
5. Run one controlled referral-digest smoke test and confirm the email appears in Sent.
6. Enable the local Windows watcher schedule and the 2 PM / 7 PM Asia/Kolkata digest schedule.
7. Only after steps 4-6 succeed, merge the prepared cloud-cutover PR that disables duplicate GitHub watcher schedules.

## GitHub ownership note

A personal-account repository has one owner. A collaborator can contribute code, but true second owner/admin control requires moving the repository into a GitHub Organization and making both people Organization Owners.

No credentials, browser cookies, resume contents, email addresses, or referral output should be committed to this repository.
