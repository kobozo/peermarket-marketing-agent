# Published terminal Meta draft republish

## Goal

Allow an operator to republish draft 156 after the founder deliberately cleared its complete Meta hierarchy, without manually changing PostgreSQL, reusing external IDs, or weakening normal approval rules.

## Current verified production state

- Draft 156 is `published` with its original Dutch creative and an approved daily budget of EUR 8.
- Its publication stores campaign `120249110304880342`, ad set `120249110305000342`, creative `28047843224854442`, and ad `120249110305530342`.
- Meta reports campaign, ad set, and ad as configured and effectively `ARCHIVED`.
- The publication replacement history already retains the older hierarchy.
- Automatic processing must continue treating an ordinary `published` draft as complete and must not replace it.

## Operator-authorized republish contract

Extend only the explicit `peermarket-meta replace-terminal-draft` command and its production service. The command may replace a `published` Meta draft when every condition below holds:

1. the operator supplies all four current Meta IDs;
2. the supplied IDs exactly equal the IDs stored for that draft;
3. Meta can read campaign, ad set, and ad;
4. all three resources have terminal configured and effective states;
5. the publication has a positive frozen approved budget;
6. draft metadata contains the complete original creative contract;
7. automatic Meta activation is enabled and connector credentials are complete.

The explicit CLI invocation is the new authorization to republish the already approved creative at its already approved budget. Slack messages, scheduled loops, retries, and ordinary approval handling cannot enter this path.

## Transaction and external workflow

After validation, atomically append the exact old IDs and observed terminal statuses to replacement history, clear only the current publication IDs/statuses, and mark one identified attempt as `creating`. Then call the existing production publication pipeline to capture a fresh screenshot/image, create a new paused campaign hierarchy, persist each new identifier, activate parent-to-child, verify configured/effective states, and retain draft status `published` after success.

The pipeline must not momentarily require or persist `approved` for this operator republish. It receives an explicit internal replacement authorization tied to the replacement attempt and exact draft ID. That authorization cannot be supplied through Slack or scheduled agent paths.

On failure, retain the replacement IDs created so far, structured failure diagnostics, and replacement history. Never restore the archived IDs as current and never automatically start another hierarchy. Slack reports success or the actionable failed phase without credentials.

Concurrent or repeated commands are safe: the guarded ID transition permits only one attempt for the exact current hierarchy. A second invocation with old IDs fails before external creation. A successful rerun requires the operator to inspect and explicitly supply the newly stored current IDs, which must themselves be fully terminal.

## Draft 156 production test

Deployment occurs only through PR and GitHub Actions. After CI, run the command on Jarvis (`192.168.1.121`) with draft 156 and its verified current IDs. Then verify:

- exactly one new replacement-history entry and one current publication row;
- new campaign, ad-set, creative, and ad IDs differ from the archived IDs;
- configured campaign/ad-set/ad status is `ACTIVE`;
- effective state is `ACTIVE`, `IN_PROCESS`, or `PENDING_REVIEW`;
- draft 156 remains `published` with the frozen EUR 8 daily budget;
- Slack contains the new Ads Manager URL and observed state;
- no second hierarchy was created by retries.

## Scope

This change republishes the existing single-language Dutch creative for draft 156. Native NL-BE/FR-BE/EN bundle generation, Meta Insights, PeerMarket attribution, and the evidence learning loop remain separate follow-up phases. No automatic budget changes, automatic republishing, database hand-editing, Meta Pixel, or Conversions API are authorized.
