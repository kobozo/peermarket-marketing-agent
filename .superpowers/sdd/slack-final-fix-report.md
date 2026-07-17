# Slack Thread Draft Revisions — Final Review Fix Report

## Findings resolved

- Root approval delivery now establishes root lineage at enqueue time and binds
  Slack identity after a successful post even when an acknowledgement changes
  the draft out of `queued` during the network call. A lost Slack response
  followed by a decision becomes terminal `obsolete` on retry instead of
  reposting forever.
- Every leased approval is atomically revalidated at the final database
  boundary before `chat.postMessage`. Approved, rejected, superseded, and
  non-latest targets are canceled as terminal `obsolete` without an external
  call. The unavoidable post-check race is documented truthfully.
- Provider/network/database operational generation errors return feedback to
  `pending` with exponential backoff and a three-attempt cap. Schema and brand
  validation failures remain `failed`; `retry_failed_feedback` provides a
  failed-only repository operation for controlled manual requeue.
- Deployment runs `peermarket-migrate` with the deployed systemd environment
  before the single restart command for either service. Command failure stops
  the workflow before restart.
- Thread acknowledgements reply on the original `thread_ts`.
- Heartbeat task database errors are logged and suppressed after the main work,
  so a committed revision remains committed and processing can continue.

## TDD evidence

Initial focused RED produced one missing repository-interface collection error
and eight behavioral failures covering root reconciliation, stale root/thread
delivery, ambiguous success, thread acknowledgement context, and deploy order.
After implementation, the focused suite passed with 38 tests; expanded revised
draft state coverage was then added for approved, rejected, and superseded
targets.

## Verification

- Baseline before changes: `302 passed in 32.97s`.
- Post-change full database-backed suite: `314 passed in 34.93s`.
- Final verification is recorded in the handoff after this report is committed.

## Self-review and residual concern

PostgreSQL cannot transact atomically with Slack. If a decision lands after the
last eligibility transaction but while/after Slack accepts the API call, the
message can already be visible. The implementation records successful delivery
when Slack returns success and prevents later retries once the target is known
stale; it does not claim to erase an already-sent message.

## Final blocker follow-up

- Replaced the privileged-user inversion in deployment with root
  `systemd-run --wait --pipe --collect --uid=peermarket-agent
  --gid=peermarket-agent`, retaining the deployed `EnvironmentFile` and working
  directory. The migration process runs as the service account, while root can
  create the transient unit unattended; migration failure stops the shell
  before either service restart.
- Strengthened the deploy contract test to reject `sudo -u peermarket-agent
  systemd-run`, require the root invocation flags/environment/directory, forbid
  failure masking, and enforce migration-before-restart ordering.
- Retryable generation failures now send concise automatic-retry copy on only
  the first attempt. The terminal validity notice is reserved for permanent
  validation failures or exhaustion of the three-attempt cap.
- A read-only SSH probe for the runner's systemd version was attempted at
  `192.168.1.121`, but runner authentication was unavailable; no production
  command or mutation occurred.

Focused RED: two failures (privilege inversion and terminal copy on transient
failure). Focused GREEN: `12 passed in 3.51s`.
