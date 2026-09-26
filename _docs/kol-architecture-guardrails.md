# KOL operational guardrails

Status: 2026-09-26. Scope: local KOL collection, review, and market context.

## Failure pattern

Recent incidents were mostly contract drift, not isolated exceptions:

| Failure | Source of drift | Guardrail |
|---|---|---|
| X cooldown expired but collection stayed blocked | Stored status and effective eligibility were projected separately | Derive effective slot readiness from cooldown, credentials, identity, and enablement in one policy service |
| Market health reported missing published symbols | Active instrument scope was used as the published manifest denominator | Compare raw/qfq coverage against the immutable publication manifest |
| Zhihu 10003 caused long cooldowns | An upstream transient response was classified as permanent incompatibility | Typed provider outcome and one retry/cooldown policy |
| Market lock interrupted a successful fetch | Posts and market metadata live in separate databases | Persist confirmed symbols in a SQLite replay table before market writes |
| Scheduled work ran at an old time or with old arguments | Multiple installers registered or removed the same task names | One task contract, one registration owner, and read-only installed-state comparison |
| Queue counts looked like missing accounts | Batch queue rows were treated as unique account gaps | Report account coverage, current batch work, and historical batch audit separately |

## Enforced now

1. `scripts/kol-task-contract.json` is the desired KOL daily schedule. Only
   `install-kol-recovery-tasks.ps1` registers KOL collection and morning tasks.
   The legacy post-fetch installer delegates to it; the research-data installer
   changes only its own market and research tasks.
2. `scripts/kol-task-doctor.ps1` checks installed triggers, actions, and run
   levels without changing them. A nonzero exit means the source contract is not
   yet active on the host.
3. The post database's `market_lead_replay` table is an outbox for confirmed
   symbols. Market lock failures cannot silently consume the only opportunity
   to queue a symbol.
4. Numbered schema migrations apply to both existing and fresh post databases.
   Contract and replay behavior have focused offline tests.
5. The doctor reads the live X policy and warns when it differs from the
   documented 180/90/60 reference. The current high-budget override is
   operator-approved and remains active; the doctor never writes policy.

## Next boundaries

- Replace provider error-string checks with typed outcomes such as
  `rate_limited`, `transient`, `auth_required`, and `identity_unverified`.
  Record the raw response separately; retry policy must use the typed outcome.
- Keep provisional NotFound/4041 accounts visible as identity gaps. Repeated
  retries should stop until a corrected profile is supplied, without removing
  the account from the coverage denominator or deleting its audit trail.
- Expose one health snapshot with distinct fields for published market
  completeness, latest completed trading day, unique account coverage, current
  batch queue, and archived batch rows. UI and CLI should consume that snapshot
  rather than calculate their own denominators.

## Acceptance

- No installer outside the KOL owner registers or deletes KOL collection tasks.
- A schedule edit changes the contract, and the doctor reports drift until the
  installed task is updated by an administrator.
- A market lock after lead extraction leaves a replay record; a later successful
  pass clears it exactly once.
- A provider failure changes account and queue state through one typed policy,
  while raw attempts and unresolved identity gaps remain auditable.
