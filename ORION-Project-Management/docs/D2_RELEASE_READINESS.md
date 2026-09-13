# D2 Release Readiness

Candidate qualification SHA: `ff7c7b0a79fbf891b61a0efd8824ed7625b48467`.

D2 Explosive-Mover Challenge, D2 report schema regression, D2 REAL Historical Replay 7D, D1/D2/D4 exact-source evidence, ORION verification, checksum freeze and artifact uploads all passed on the qualified candidate.

The candidate is qualified but is not approved for direct merge to `main` because `main` and the candidate have diverged. `main` is 13 commits ahead of the merge base while the candidate is 360 commits ahead. Integration must therefore be performed from the current `main` line and reverified.

PR #8 remains a draft verification anchor and is not the production merge vehicle.
