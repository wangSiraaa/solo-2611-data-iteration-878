"""Unit tests for maintenance settlement, vouchers and correction advice.

Run:  python3 -m unittest discover -s solution/tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage_catalog import Interval  # noqa: E402
from handover_book import HandoverPair, HandoverRegistry, HandoverVersion  # noqa: E402
from ground_lineage import LineageTracer  # noqa: E402
from maintenance_ledger import (  # noqa: E402
    Contract,
    ContractZone,
    PaymentLedger,
    SettlementBatch,
    SettlementEngine,
    SettlementError,
    VoucherLine,
    advise_correction,
)


def pair(a, b, c, d):
    return HandoverPair(Fraction(a), Fraction(b), Fraction(c), Fraction(d))


def canonical_registry():
    """S0 axis 8, S1 axis 9 with the task's worked-example handover."""
    reg = HandoverRegistry()
    reg.add(HandoverVersion("H-v1", "S0", "S1", Fraction(8), Fraction(9), (
        pair(0, 3, 0, 3), pair(5, 8, 6, 9),
    )))
    reg.add(HandoverVersion("H-v2", "S0", "S1", Fraction(8), Fraction(9), (
        pair(0, 3, 0, 3), pair(5, 8, 5, 8),
    ), supersedes="H-v1"))
    return reg


def engine():
    reg = canonical_registry()
    return reg, SettlementEngine(reg, LineageTracer(reg))


# ---------------------------------------------------------------------------
# The three canonical worked-example amounts
# ---------------------------------------------------------------------------


class TestCanonicalExample(unittest.TestCase):
    def setUp(self):
        self.reg, self.engine = engine()
        self.contract = Contract("S1", (
            ContractZone("Z", Fraction(0), Fraction(8), Fraction(2)),
        ))
        self.report = [Interval(Fraction(0), Fraction(8))]
        self.pins = {("S0", "S1"): "H-v1"}

    def test_first_settlement_pays_ten(self):
        batch, _ = self.engine.settle_batch(
            "B", "S1", [("R", "S0", self.report)],
            self.contract, PaymentLedger(), self.pins,
        )
        self.assertEqual(batch.total_amount, Fraction(10))

    def test_already_paid_ground_deducted_pays_eight(self):
        prior = PaymentLedger([VoucherLine(
            voucher_id="P", report_id="P", target_season="S1",
            target_interval=Interval(Fraction(6), Fraction(7)),
            birth_season="S0", birth_interval=Interval(Fraction(5), Fraction(6)),
            zone_id="Z", length=Fraction(1), unit_price=Fraction(2),
            amount=Fraction(2), handover_versions=("H-v1",),
        )])
        batch, _ = self.engine.settle_batch(
            "B", "S1", [("R", "S0", self.report)],
            self.contract, prior, self.pins,
        )
        self.assertEqual(batch.total_amount, Fraction(8))

    def test_corrected_book_empty_ledger_pays_twelve(self):
        batch, _ = self.engine.settle_batch(
            "B", "S1", [("R", "S0", self.report)],
            self.contract, PaymentLedger(), {("S0", "S1"): "H-v2"},
        )
        self.assertEqual(batch.total_amount, Fraction(12))

    def test_new_build_middle_section_never_paid(self):
        # The new [3,6) middle ground reuses an old label but is new build.
        batch, _ = self.engine.settle_batch(
            "B", "S1",
            [("R", "S1", [Interval(Fraction(3), Fraction(6))])],
            self.contract, PaymentLedger(), self.pins,
        )
        self.assertEqual(batch.total_amount, Fraction(0))


# ---------------------------------------------------------------------------
# Contract clipping and exact rational amounts
# ---------------------------------------------------------------------------


class TestContract(unittest.TestCase):
    def test_overlapping_zones_rejected(self):
        with self.assertRaises(SettlementError):
            Contract("S", (
                ContractZone("a", Fraction(0), Fraction(5), Fraction(1)),
                ContractZone("b", Fraction(4), Fraction(9), Fraction(1)),
            ))

    def test_rational_zone_clipping(self):
        reg, eng = engine()
        contract = Contract("S1", (
            ContractZone("lo", Fraction(0), Fraction(7, 2), Fraction(2)),
            ContractZone("hi", Fraction(7, 2), Fraction(8), Fraction(1)),
        ))
        batch, _ = eng.settle_batch(
            "B", "S1",
            [("R", "S0", [Interval(Fraction(0), Fraction(8))])],
            contract, PaymentLedger(), {("S0", "S1"): "H-v1"},
        )
        # Inherited payable target ground under H-v1: [0,3) and [6,8).
        # [0,3) lies wholly in lo: 3*2 = 6 ; [6,8) wholly in hi: 2*1 = 2.
        self.assertEqual(batch.total_amount, Fraction(8))


# ---------------------------------------------------------------------------
# Once-only payment and explicit in-batch order
# ---------------------------------------------------------------------------


class TestAllocationOrder(unittest.TestCase):
    def setUp(self):
        self.reg, self.eng = engine()
        self.contract = Contract("S1", (
            ContractZone("Z", Fraction(0), Fraction(9), Fraction(1)),
        ))
        self.pins = {("S0", "S1"): "H-v1"}

    def test_first_report_owns_the_overlap(self):
        # Both reports cover the same inherited target ground [0,3).
        first, _ = self.eng.settle_batch(
            "B", "S1",
            [("R1", "S0", [Interval(Fraction(0), Fraction(3))]),
             ("R2", "S0", [Interval(Fraction(0), Fraction(3))])],
            self.contract, PaymentLedger(), self.pins,
        )
        by_report = {}
        for line in first.lines:
            by_report.setdefault(line.report_id, []).append(line)
        self.assertEqual(sum(l.length for l in by_report["R1"]), Fraction(3))
        self.assertNotIn("R2", by_report)  # second report gets nothing

    def test_reversing_order_reverses_ownership(self):
        second, _ = self.eng.settle_batch(
            "B", "S1",
            [("R2", "S0", [Interval(Fraction(0), Fraction(3))]),
             ("R1", "S0", [Interval(Fraction(0), Fraction(3))])],
            self.contract, PaymentLedger(), self.pins,
        )
        owners = {line.report_id for line in second.lines}
        self.assertEqual(owners, {"R2"})

    def test_cross_batch_ledger_blocks_repeat_payment(self):
        b1, ledger = self.eng.settle_batch(
            "B1", "S1",
            [("R1", "S0", [Interval(Fraction(0), Fraction(3))])],
            self.contract, PaymentLedger(), self.pins,
        )
        b2, _ = self.eng.settle_batch(
            "B2", "S1",
            [("R2", "S0", [Interval(Fraction(0), Fraction(3))])],
            self.contract, ledger, self.pins,
        )
        self.assertEqual(b1.total_amount, Fraction(3))
        self.assertEqual(b2.total_amount, Fraction(0))


# ---------------------------------------------------------------------------
# Offline replay of a signed batch
# ---------------------------------------------------------------------------


class TestReplay(unittest.TestCase):
    def test_replay_reproduces_lines(self):
        reg, eng = engine()
        contract = Contract("S1", (
            ContractZone("Z", Fraction(0), Fraction(8), Fraction(2)),
        ))
        pins = {("S0", "S1"): "H-v1"}
        report_index = {"R": ("S0", [Interval(Fraction(0), Fraction(8))])}
        batch, _ = eng.settle_batch(
            "B", "S1", [("R", "S0", report_index["R"][1])],
            contract, PaymentLedger(), pins,
        )
        ok, diffs = eng.replay_batch(batch, report_index, PaymentLedger())
        self.assertTrue(ok, diffs)

    def test_json_roundtrip_then_replay(self):
        reg, eng = engine()
        contract = Contract("S1", (
            ContractZone("Z", Fraction(0), Fraction(8), Fraction(2)),
        ))
        pins = {("S0", "S1"): "H-v1"}
        ivs = [Interval(Fraction(0), Fraction(8))]
        batch, _ = eng.settle_batch(
            "B", "S1", [("R", "S0", ivs)],
            contract, PaymentLedger(), pins,
        )
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "batch.json")
            batch.save_json(path)
            reloaded = SettlementBatch.load_json(path)
        ok, _ = eng.replay_batch(
            reloaded, {"R": ("S0", ivs)}, PaymentLedger()
        )
        self.assertTrue(ok)
        self.assertEqual(reloaded.total_amount, batch.total_amount)


# ---------------------------------------------------------------------------
# Correction advice: debit/credit and the no-double-pay block
# ---------------------------------------------------------------------------


class TestCorrectionAdvice(unittest.TestCase):
    def setUp(self):
        self.reg, self.eng = engine()
        self.contract = Contract("S1", (
            ContractZone("Z", Fraction(0), Fraction(9), Fraction(2)),
        ))
        self.ivs = [Interval(Fraction(0), Fraction(8))]
        self.pins_v1 = {("S0", "S1"): "H-v1"}
        self.pins_v2 = {("S0", "S1"): "H-v2"}

    def _batch(self):
        batch, _ = self.eng.settle_batch(
            "B", "S1", [("R", "S0", self.ivs)],
            self.contract, PaymentLedger(), self.pins_v1,
        )
        return batch

    def test_debit_and_credit_per_voucher(self):
        batch = self._batch()
        advice = advise_correction(
            self.eng, batch, {"R": ("S0", self.ivs)},
            PaymentLedger(), self.pins_v2,
        )
        kinds = {a.kind for a in advice.per_voucher}
        self.assertIn("debit", kinds)
        self.assertIn("credit", kinds)
        # Old batch is untouched and still sums to its signed amount.
        self.assertEqual(batch.total_amount, Fraction(12))
        # Same priced length moves from target [8,9) to [5,6): equal-rate
        # zones mean the cash total is conserved (debit and credit cancel).
        self.assertEqual(advice.net_cash, Fraction(0))
        debit = sum(a.amount for a in advice.per_voucher if a.kind == "debit")
        credit = sum(a.amount for a in advice.per_voucher if a.kind == "credit")
        self.assertEqual(debit, Fraction(2))
        self.assertEqual(credit, Fraction(2))

    def test_blocked_credit_when_external_voucher_holds_ground(self):
        # Use a price boundary inside the moved ground so the corrected
        # re-simulation genuinely creates an extra credit (not just a move).
        graded = Contract("S1", (
            ContractZone("lo", Fraction(0), Fraction(6), Fraction(2)),
            ContractZone("hi", Fraction(6), Fraction(9), Fraction(3)),
        ))
        batch, _ = self.eng.settle_batch(
            "B", "S1", [("R", "S0", self.ivs)],
            graded, PaymentLedger(), self.pins_v1,
        )
        # The corrected book lands target [5,6) (lo zone, birth S0 [5,6)).
        external = PaymentLedger([VoucherLine(
            voucher_id="EXT", report_id="OTHER", target_season="S1",
            target_interval=Interval(Fraction(5), Fraction(6)),
            birth_season="S0", birth_interval=Interval(Fraction(5), Fraction(6)),
            zone_id="lo", length=Fraction(1), unit_price=Fraction(2),
            amount=Fraction(2), handover_versions=("H-v2",),
        )])
        advice = advise_correction(
            self.eng, batch, {"R": ("S0", self.ivs)},
            PaymentLedger(), self.pins_v2, confirmed_ledger=external,
        )
        blocked = [a for a in advice.per_voucher if a.kind == "blocked"]
        self.assertTrue(blocked)
        self.assertEqual(advice.blocked_amount, Fraction(2))
        # The blocked credit is excluded from net cash.
        self.assertEqual(
            advice.net_cash,
            sum(a.amount for a in advice.per_voucher if a.kind == "credit")
            - sum(a.amount for a in advice.per_voucher if a.kind == "debit"),
        )

    def test_old_vouchers_are_not_mutated(self):
        batch = self._batch()
        before = [ln.to_dict() for ln in batch.lines]
        advise_correction(
            self.eng, batch, {"R": ("S0", self.ivs)},
            PaymentLedger(), self.pins_v2,
        )
        self.assertEqual([ln.to_dict() for ln in batch.lines], before)


if __name__ == "__main__":
    unittest.main()
