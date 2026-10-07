"""The sealed line-wide orientation for rebooks.

Two orientations were on the table:

* ``recall_all_then_reroute`` — pull every in-transit bag back before any
  rerouting;
* ``commit_point_cutoff`` — only re-target bags still before their last
  physically recallable cell; bags past that point ride to completion.

The chosen one is journaled (``policy.put``) and written into the audit
chain, so every later verdict can be attributed to a recorded decision with
its rejected alternative and trade-offs attached.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.clock import Clock
from bagsort.model.entities import RebookPolicy
from bagsort.store.store import Store

COMMIT_POINT_CUTOFF = "commit_point_cutoff"
RECALL_ALL_THEN_REROUTE = "recall_all_then_reroute"

RATIONALE = (
    "only re-target a bag while its armed divert can still be cancelled; "
    "bags past the recall point physically cannot be pulled back, so they "
    "ride to the old chute and become manual transfer work instead of a "
    "second automated sort"
)

TRADE_OFFS = (
    "accepted: zero double-sorts, no loop stoppage, bags keep their flight "
    "deadline because the belt never backs up; "
    "rejected alternative would recall everyone (even bags already falling "
    "into a chute, which is physically impossible), re-sort the whole batch "
    "(repeat handling, extra load), and block the loop while the pullback "
    "runs (missed cutoffs); "
    "cost accepted: past-point bags need a manual transfer order from the "
    "old chute, and the field sees two verdicts (recalled vs let-ride) that "
    "reconciliation must attribute"
)


@dataclass(frozen=True)
class PolicyDecision:
    policy: RebookPolicy
    already_sealed: bool


class PolicyBook:
    """Seals the chosen orientation exactly once."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def current(self) -> RebookPolicy | None:
        return self._store.state().rebook_policy

    def seal(self) -> PolicyDecision:
        existing = self._store.state().rebook_policy
        if existing is not None:
            return PolicyDecision(policy=existing, already_sealed=True)
        policy = RebookPolicy(
            name=COMMIT_POINT_CUTOFF,
            rejected=RECALL_ALL_THEN_REROUTE,
            rationale=f"{RATIONALE}; trade-offs: {TRADE_OFFS}",
            sealed_at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.rebook_policy = policy
            session.record("policy.put", {"policy": policy.as_dict()}, policy.sealed_at)
        return PolicyDecision(policy=policy, already_sealed=False)
