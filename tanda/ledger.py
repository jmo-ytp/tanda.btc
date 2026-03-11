"""
Persistent debt ledger for a tanda LN round.

Tracks per-participant:
  - accumulated debt (sats owed from missed contributions)
  - whether they have already received their pot

Rules enforced by the coordinator:
  - A participant with debt > 0 cannot receive their pot turn until debt is cleared.
  - Once the winner clears their debt (via a separate hold invoice), the round proceeds.
  - Participants who already received their pot and later miss a payment accumulate
    a "social debt" that they can pay off at any time via the late-payment flow.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ParticipantRecord:
    debt_sats: int = 0
    pot_received: bool = False
    rounds_missed: list[int] = field(default_factory=list)
    rounds_paid: list[int] = field(default_factory=list)


class Ledger:
    def __init__(self, n: int, path: str | None = None):
        self.n = n
        self._path = Path(path) if path else None
        self.records: list[ParticipantRecord] = [ParticipantRecord() for _ in range(n)]
        if self._path and self._path.exists():
            self._load()

    # ── Mutations ──────────────────────────────────────────────────────────────

    def record_missed(self, idx: int, round_idx: int, sats: int) -> None:
        """Participant idx missed their contribution in round_idx."""
        rec = self.records[idx]
        rec.debt_sats += sats
        if round_idx not in rec.rounds_missed:
            rec.rounds_missed.append(round_idx)
        self._save()

    def record_paid(self, idx: int, round_idx: int) -> None:
        rec = self.records[idx]
        if round_idx not in rec.rounds_paid:
            rec.rounds_paid.append(round_idx)
        self._save()

    def apply_payment(self, idx: int, sats: int) -> int:
        """Apply sats toward participant's debt. Returns remaining debt."""
        rec = self.records[idx]
        rec.debt_sats = max(0, rec.debt_sats - sats)
        self._save()
        return rec.debt_sats

    def mark_pot_received(self, idx: int) -> None:
        self.records[idx].pot_received = True
        self._save()

    # ── Queries ────────────────────────────────────────────────────────────────

    def debt(self, idx: int) -> int:
        return self.records[idx].debt_sats

    def has_received_pot(self, idx: int) -> bool:
        return self.records[idx].pot_received

    def is_eligible(self, idx: int) -> bool:
        """Participant can receive their pot only when debt is zero."""
        return self.records[idx].debt_sats == 0

    def summary(self) -> str:
        lines = ["Ledger state:"]
        for i, r in enumerate(self.records):
            status = []
            if r.debt_sats:
                status.append(f"debt={r.debt_sats} sats (missed rounds {r.rounds_missed})")
            else:
                status.append("clear")
            status.append("pot received" if r.pot_received else "awaiting pot")
            lines.append(f"  P{i}: {', '.join(status)}")
        return "\n".join(lines)

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps([asdict(r) for r in self.records], indent=2)
            )

    def _load(self) -> None:
        data = json.loads(self._path.read_text())
        self.records = [ParticipantRecord(**d) for d in data]
