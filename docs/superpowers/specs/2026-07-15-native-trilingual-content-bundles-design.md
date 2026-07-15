# Native Trilingual Content Bundles Design

## Goal

Every scheduled marketing concept produces a linked bundle of three independently
authored variants: Belgian Dutch (`NL-BE`), Belgian French (`FR-BE`), and English
(`EN`). Copy must read as native marketing writing, not as a literal translation
from English. Each variant receives its own quality decision and explicit human
approval.

## Shared brief, independent writing

The agent first creates one language-neutral campaign brief containing only
facts and constraints: action type, topic, audience, offer, permitted claims,
CTA intent, landing page, campaign objective, and paid-media budget policy. The
brief contains no English source copy for the language writers to translate.

Three generation calls consume that brief independently. Variants share a
`content_bundle_id` and factual brief version, but each stores its own copy,
structured metadata, cost, scores, revision lineage, and decision state.

## Locale constitutions

Each writer receives a locale constitution in addition to the PeerMarket brand
voice:

- `NL-BE`: idiomatic Belgian Dutch, natural word order, correct articles and
  prepositions, restrained marketing language, Belgian marketplace vocabulary,
  and no calques from English. Avoid Netherlands-specific phrasing when a common
  Belgian form exists.
- `FR-BE`: idiomatic Belgian French, natural register and syntax, Belgian usage
  where relevant, correct typography and contractions, and no Dutch/English
  calques.
- `EN`: natural international English suitable for a Belgian audience, not a
  source template for the other locales.

CTA enums required by external platforms remain platform values and are not
treated as visible mixed-language copy. Visible CTA labels and body copy follow
the target locale.

## Native-language quality gate

Every variant runs through two independent gates:

1. the existing PeerMarket brand-quality and truthfulness validation;
2. a locale-quality reviewer using a separate deterministic prompt.

The locale reviewer returns structured scores for grammar, idiomaticity,
locale fit, and literal-translation risk, plus concise evidence. Every category
must score at least 85 and the result must contain no critical grammar error.
The reviewer is explicitly told that plausible but English-shaped syntax must
fail. Output-schema and platform-length validation still run in code.

A failed variant is retained as internal evidence but never enters the approval
queue. The agent regenerates only that locale, at most twice per scheduled run.
After two failed regenerations it alerts the founder and leaves the other
passing locales available for approval.

## Scheduling and bundles

The daily plan expands each selected action concept into exactly three locales
in fixed order: `NL-BE`, `FR-BE`, `EN`. For the current Meta, TikTok, and email
plan this produces at most nine approval drafts per day. The audience/theme and
campaign brief are selected once per bundle so language variants do not drift
into different offers or strategies.

Idempotency is keyed by schedule date, action type, and bundle brief version.
A retry fills missing/failed locales only; it does not regenerate passing or
already-decided variants.

## Approval and publication

Slack groups the three variants under one bundle summary and posts one approval
message per locale. Each variant is independently queued, revised, approved,
rejected, and published. No all-or-nothing bundle decision exists. Approval of
one locale never authorizes another locale or increases a paid budget.

For paid Meta content, each approved locale becomes its own ad under an
appropriate locale-specific ad set or targeting configuration. English must not
silently inherit the current Dutch/French locale targeting. Targeting for a
locale must explicitly match that locale before publication; unsupported locale
targeting blocks only that variant.

## Learning and feedback

Performance and revision learnings retain locale as a required dimension. The
agent may share factual learnings across a bundle but must not copy winning
phrasing from one language into another through translation. Language-quality
failures and founder revision feedback become evidence for that locale's future
prompts.

## Data model

The agent database adds:

- `content_bundles`: schedule date, action type, brief JSON, brief version,
  common audience/theme, and timestamps;
- bundle linkage and `locale` on drafts;
- `locale_quality`: structured category scores, evidence, reviewer model, and
  pass/fail;
- uniqueness on bundle plus locale plus revision root where appropriate.

Existing `NL`, `FR`, and `EN` drafts remain valid legacy records. New generation
uses canonical locale codes while platform adapters map them to supported API
language/locale identifiers.

## Today's rollout

After CI deployment, an explicit catch-up command creates today's missing
bundles for Meta, TikTok, and email. It is idempotent and runs the same generation
and quality path as the scheduler. It does not auto-approve or publish any
variant. Since draft 156 has been archived, it is excluded from reuse and no
replacement campaign is created automatically.

## Testing and deployment

Tests cover shared-brief consistency, independent generation calls, locale
constitutions, English-calque rejection in Dutch/French, grammar thresholds,
per-locale retry limits, partial bundle success, schedule idempotency, separate
approval, revision lineage, locale-specific Meta targeting refusal, and today's
catch-up idempotency.

No new secret is required. Configuration for locales, gate thresholds, and
retry count is non-sensitive and uses GitHub repository variables only if it
must be runtime-configurable. Deployment and catch-up use the existing CI and
operator CLI paths.
