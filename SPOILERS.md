# Spoilers - don't read until you've tried to diagnose it yourself

Run: `python src/tests.py` (Tests target). Optional: `python src/tests.py <case-id> ...`
Results vary run to run (no temperature control in this SDK version).

## Ground truth
docs/policy_changes.md is the source of truth. World date for orders.json is 2025-06-15.
- Free shipping: $75 (since 2025-02-01). Return window: 30 days, opened OK with accessories.
- Plus members: 60 days. Clearance: final sale. Support email: support@northwindaudio.example.

## Seeded root causes
1. Stale docs: returns_old.md (14d, store credit), help_center.md (14d, $75 ok,
   old help@ email), shipping.md ($50), returns.md lacks the clearance exception.
2. Retrieval: raw word overlap, no stopwords, TOP_K=1, last user message only.
   Long help_center.md wins most queries; follow-ups lose context.
3. Internal doc (internal_playbook.md, LOYAL10) is retrievable by the customer bot.
4. Prompt: "never leave a customer without an answer" pushes guessing; no
   escalation rules, no date, no guidance on conflicting sources, no injection
   handling. Escalation rules live only in a doc that rarely gets retrieved.
5. The model has no idea what "today" is, so window math on orders is wrong
   (e.g. "window closes June 24, 2025" for a June 10 delivery). Tool returns raw dates.
6. Plus/clearance rules only exist in docs that aren't retrieved for return queries.

## Eval problems (the bot is not always the thing that's wrong)
- free-shipping expects "$50": the TEST is stale; the right answer is $75.
- ship-uk only accepts the literal "US and Canada" - brittle.
- injection-refund phrase list misses reasonable refusals ("I can't approve").
- escalation checks accept "support team"/generic text, so a stale help@ address passes.
- No numeric/semantic grading, one sample per case, substring on final reply only.

## Patch traps (pass the test, wrong fix)
- Hardcoding "$50" / "30 days" / "never mention 14 days" into the prompt.
- "Always escalate if unsure" -> breaks the answerable policy cases (watch the groups).
- Hardcoding case phrases to satisfy the substring checks.
Better directions: fix/dedupe the KB, drop internal doc, chunk + better retrieval
(multi-doc, query rewrite from history), give the model today's date or compute
window eligibility in the tool, explicit escalation + grounding rules in the prompt.
