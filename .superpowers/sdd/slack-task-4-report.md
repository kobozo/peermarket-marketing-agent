# Slack revisions Task 4 report

## Implemented

- Added an action-aware revision prompt with escaped, delimited source/feedback data, immutable action/language requirements, non-approval/non-spend semantics, protected-field instructions, and an exact per-action JSON contract plus `change_summary`.
- Added `revise_draft()` with exact-field/type/length validation for TikTok, email, SEO, and Meta; structured metadata persistence; immutable channel/action/language; Meta CTA enum normalization; and explicit-feedback checks for protected audience, CTA, and budget changes.
- Added repository orchestration helpers for ready-thread discovery, latest queued-source loading, and sanitized failed-batch recording while retaining the existing 15-second claim and atomic persistence/supersede/application transaction.
- Added a dedicated 15-second worker that claims one frozen batch, generates and scores against the existing brand voice gate, persists valid variants, enqueues same-thread approval messages, and leaves feedback arriving during generation pending for the next batch.
- Wired startup processing and the recurring revision worker into the agent entrypoint.

## TDD evidence

- Initial prompt/generator test run failed at collection because the new modules did not exist.
- Initial loop test run failed at collection because the loop module did not exist.
- Focused post-implementation run: `22 passed` across prompt, generator, loop, repository, and agent-main tests.
- Full suite before final lint correction: `275 passed in 28.67s`.

## Coverage highlights

- Source draft and founder feedback are escaped and delimited as untrusted data; feedback cannot approve, publish, or authorize spend.
- Exact schemas and structured metadata for all four supported actions.
- Protected Meta audience/CTA/budget preservation unless feedback explicitly requests the relevant change; a changed budget exists only on the new queued draft.
- Malformed generation and scores below 80 mark claimed feedback failed without superseding.
- Successful lineage persistence, predecessor superseding, and feedback application are atomic before idempotent same-thread outbox enqueue.
- Feedback received during model I/O remains pending and never enters the frozen in-flight prompt.
- Repeated workers do not duplicate generation.

## Final verification

See the task commit/report handoff for the fresh full-suite, Ruff, and `git diff --check` results run immediately before commit.

## Review-blocker hardening

- Moved revised draft insertion, predecessor superseding, feedback application, and frozen same-thread outbox insertion into one mandatory database transaction. An injected outbox idempotency conflict proves the draft/feedback transitions roll back together.
- Added durable root-generation leases plus per-feedback owner, expiry, and attempt state. Claims reclaim stale processing on startup, reject competing owners, renew during model I/O without retaining a database connection, and release on success/failure/conflict. Cancellation immediately requeues its owned batch; crash recovery occurs after lease expiry.
- Added owner checks to finalization so a stale worker cannot fail or persist feedback reclaimed by a newer worker. Latest-leaf conflicts are requeued rather than recorded as generation failures.
- Replaced protected-field substring matching with direct, affirmative command recognition in English, Dutch, and French. Negations, incidental mentions, quoted source text, and instruction-injection phrases fail closed. Meta audience, CTA, and spend changes remain forbidden unless their specific affirmative intent is proven.
- Extracted shared canonical validators used by both normal generation and revision generation. Runtime validation now consistently enforces TikTok word/punctuation/em-dash limits, email subject/body/plain-text limits, SEO title suffix and length rules, and Meta audience/CTA/budget/copy constraints.
- Added concurrent/in-flight tests proving feedback B cannot join A's frozen prompt and, after A finalizes, B loads A's newly queued leaf to create revision 2.

Review hardening focused verification reached `68 passed`; the first fresh full run after these changes reached `297 passed in 30.61s`. A final verification gate is run again immediately before the hardening commit.

Final hardening verification after making the transactional outbox arguments mandatory: `297 passed in 32.63s`; Ruff format check reported all 86 files formatted, Ruff lint passed, and the staged diff check passed.
