# Meta Page Identity Design

## Problem

Meta rejects draft publishing because the ad creative's `object_story_spec` has
`link_data` but no Facebook Page identity. The current error wrapper also drops
Meta's error code, subcode, and user-facing diagnostic fields.

## Design

Add a required `page_id` field to `MetaConfig`, sourced from `META_PAGE_ID`.
Pass it as `object_story_spec.page_id` when creating the link creative. Treat a
missing Page ID like the other missing Meta credentials so the pipeline fails
before creating partial campaign resources.

Store `META_PAGE_ID=61592144690879` as a GitHub Actions repository secret. The
deploy workflow will expose it to the deploy job and write it into
`/etc/peermarket-agent/secrets.env`. Deployment remains exclusively the existing
push-to-`main` GitHub Actions workflow.

Expand `MetaAdsError` formatting to retain the API message and, when supplied by
the SDK, the error code, subcode, user title, and user message. Do not include
request credentials or tokens.

## Testing

Regression tests will first demonstrate that creative creation lacks the Page
ID and that missing Page configuration is not rejected. Implementation will then
make those tests pass. Existing Meta connector, configuration, pipeline, lint,
format, and full test suites must pass before publishing.

## Deployment verification

Commit and push the implementation through GitHub, observe the deploy workflow
on the self-hosted `agent-peermarket` runner, and require its service restart and
health check to succeed. Do not automatically retry draft 156 because doing so
creates external Meta resources; retry requires a separate explicit request.
