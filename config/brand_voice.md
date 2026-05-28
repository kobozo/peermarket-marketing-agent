# PeerMarket brand voice

## Identity (non-negotiable)

PeerMarket is a Belgian secondhand marketplace whose differentiator is **verified-identity trust** via Stripe Identity. Our voice reflects that:

- Belgian roots. Dry, slightly understated humor. Never American hype.
- Trust + verified identity are the wedge. Lean into them, never soften.
- Local-first. Comparisons to Marktplaats and Facebook Marketplace are fair game in organic content.

## Languages

- NL (Dutch) and FR (French) are primary, with full parity at the **program level** — across published items, we maintain rough parity between NL and FR output. Individual drafts are single-language; pairing happens at publish-time when a campaign goes live, not within a single draft.
- EN allowed for SEO landing pages and developer-facing surfaces only.
- Use natural NL/FR. Avoid translation-sounding sentences. A Belgian reader should recognize the dialect.

### Platform-enforced text (out of scope for language evaluation)

Some advertising platforms enforce fixed English strings that cannot be translated:

- **Meta Ads CTA labels** are a fixed enum: `Learn More`, `Sign Up`, `Shop Now`, `Get Started`. These are platform UI labels, not creative copy. Do not penalize an NL or FR ad for using one of these.
- Targeting IDs, audience profile keys, ad account identifiers are platform metadata, not user-facing text — out of scope.

When scoring a draft, evaluate only the **creative copy** (headline, description, primary text, hook, body, CTA copy that isn't a fixed platform enum). Ignore CTA-label fields that match the Meta enum above and any line beginning with `Audience:`, `Suggested daily budget:`, or `CTA:` followed by a Meta enum value.

## Tone rules

- No em-dashes (—). Use commas or short sentences.
- No exclamation marks unless quoting a user.
- No emoji floods. One emoji per post max, and only when it adds meaning.
- Sentences average ≤ 18 words. Long compound thoughts get split.
- Address the reader as "je" (NL) / "tu" (FR) for casual posts, "u" / "vous" for formal/legal copy.

## Banned phrases

- "Game-changer", "revolutionary", "best ever", "unique opportunity"
- "Don't miss out", "act now", "limited time" (high-pressure language)
- Any phrasing that implies certified outcomes for resale value, identity verification rejection, or legal advice
- Competitor brand names in **paid ad copy** (organic comparison content is OK)

## Visual truthfulness (non-negotiable, spec §16)

- Never generate synthetic depictions of products, people, or transactions.
- Permitted visuals: real peermarket.eu screenshots, real photos with consent, abstract branded graphics (typography/color/icons), Pillow/Recraft frame assets, free-license stylized stock.
- Recraft is constrained to brand-graphic styles only — no photorealistic humans, no product mockups.

## Approved-example seed (Phase 1a)

The library grows as drafts get approved. Seed examples:

### TikTok organic — NL declutter
> Marktplaats moe? Verkoop veilig op PeerMarket. Stripe-ID badge, geen lokvogels.

### TikTok organic — FR vide grenier
> Marre des arnaques sur Marktplaats? Vends en sécurité sur PeerMarket. Identité vérifiée par Stripe.

### Email re-engagement — NL
> Subject: Je hebt nog niets verkocht
> Body: Je hebt een account maar nog geen plaatsing. Zonde — die jas in de kast is voor iemand anders een vondst. We helpen je listingen in 3 minuten. [Plaats nu]

### SEO meta tag — /how-it-works (NL)
> Title: Veilig tweedehands kopen en verkopen — PeerMarket
> Description: Geverifieerde verkopers, transparante prijzen, geen Marktplaats-stress. Plaats je eerste item gratis.

---

*This file is the source of truth. The `brand_voice` DB row is synced from this on every service boot. Edit this file via PR — don't update the DB directly.*
