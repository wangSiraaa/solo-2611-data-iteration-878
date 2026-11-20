"""Scale profile for the cross-season metering layer.

Simulates the stated peak load: many measurement seasons, hundreds of
published reports repeatedly traced across the handover chain, plus a small
number of corrections that must be re-followable several seasons back.

Run:  python3 solution/bench_cross_season.py
"""

from __future__ import annotations

import os
import random
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage_catalog import Interval  # noqa: E402
from handover_book import HandoverPair, HandoverRegistry, HandoverVersion  # noqa: E402
from ground_lineage import LineageTracer  # noqa: E402
from maintenance_ledger import (  # noqa: E402
    Contract,
    ContractZone,
    PaymentLedger,
    SettlementEngine,
)


SEASONS = 6
AXIS = 200
REPORTS = 300
TRACE_QUERIES = 400


def build_books(seed: int = 11) -> HandoverRegistry:
    rnd = random.Random(seed)
    reg = HandoverRegistry()
    prev = f"Q0"
    for s in range(1, SEASONS):
        cur = f"Q{s}"
        # Tile most of the axis into equal-length, slightly shifted pairs;
        # leave deterministic gaps so retired/new-build ground exists.
        pairs = []
        cursor = 0
        shift = rnd.randint(-2, 2)
        while cursor + 4 <= AXIS:
            if rnd.random() < 0.75:
                new_lo = max(0, min(AXIS - 4, cursor + shift))
                pairs.append(
                    HandoverPair(
                        Fraction(cursor), Fraction(cursor + 4),
                        Fraction(new_lo), Fraction(new_lo + 4),
                    )
                )
            cursor += 4
        # De-duplicate any colliding new sides by skipping overlaps (the
        # registry validates disjointness, so build a clean subset).
        clean, used_new, used_old = [], [], []
        for p in pairs:
            if any(not (p.new_end <= u.start or u.end <= p.new_start) for u in used_new):
                continue
            if any(not (p.old_end <= u.start or u.end <= p.old_start) for u in used_old):
                continue
            clean.append(p)
            used_new.append(p.new_interval)
            used_old.append(p.old_interval)
        reg.add(HandoverVersion(
            f"H{prev}{cur}-v1", prev, cur,
            Fraction(AXIS), Fraction(AXIS), tuple(clean),
        ))
        prev = cur
    return reg


def build_reports(rnd: random.Random):
    reports = []
    for i in range(REPORTS):
        start = rnd.randrange(0, AXIS - 10)
        length = rnd.randint(2, 10)
        reports.append((
            f"R{i:03d}", "Q0",
            [Interval(Fraction(start), Fraction(min(start + length, AXIS)))],
        ))
    return reports


def main() -> int:
    rnd = random.Random(23)
    reg = build_books()
    tracer = LineageTracer(reg)
    engine = SettlementEngine(reg, tracer)
    pins = {
        (f"Q{s}", f"Q{s+1}"): f"HQ{s}Q{s+1}-v1"
        for s in range(SEASONS - 1)
    }
    last_season = f"Q{SEASONS - 1}"
    reports = build_reports(rnd)
    contract = Contract(last_season, (
        ContractZone("all", Fraction(0), Fraction(AXIS), Fraction(3)),
    ))

    print("=" * 72)
    print(f"Cross-season scale: {SEASONS} seasons, axis {AXIS}, "
          f"{REPORTS} reports, {TRACE_QUERIES} repeat traces")
    print("=" * 72)

    # 1. Repeated cross-season trace queries (the hot read path).
    t0 = time.perf_counter()
    for _ in range(TRACE_QUERIES):
        rid, season, ivs = reports[rnd.randrange(len(reports))]
        target = f"Q{rnd.randint(1, SEASONS - 1)}"
        tracer.trace_forward(season, ivs[0], target, pins)
    trace_secs = time.perf_counter() - t0
    print("\n[1] cross-season trace queries (multi-edge, indexed clipping)")
    print(f"    {trace_secs*1000:.1f} ms total, "
          f"{trace_secs/TRACE_QUERIES*1e6:.1f} us/query")

    # 2. Full settlement of every report onto the last season.
    t1 = time.perf_counter()
    ledger = PaymentLedger()
    total_amount = Fraction(0)
    paid_reports = 0
    for rid, season, ivs in reports:
        batch, ledger = engine.settle_batch(
            f"B-{rid}", last_season, [(rid, season, ivs)],
            contract, ledger, pins,
        )
        if batch.total_amount:
            paid_reports += 1
        total_amount += batch.total_amount
    settle_secs = time.perf_counter() - t1
    print("\n[2] settlement of every report (once-only deduction accumulates)")
    print(f"    {settle_secs*1000:.1f} ms total, "
          f"{settle_secs/REPORTS*1e3:.3f} ms/report; "
          f"{paid_reports} reports earned payment")
    print(f"    ledger holds {len(ledger.lines)} voucher lines; "
          f"total amount {total_amount}")

    # 3. A correction on the first edge, re-traced through every later season.
    edge0 = ("Q0", "Q1")
    head = reg.current(*edge0)
    corrected = HandoverVersion(
        "HQ0Q1-v2", "Q0", "Q1",
        head.length_from, head.length_to,
        head.pairs[:max(1, len(head.pairs) - 1)],  # drop one pair: more retired
        supersedes=head.version_id,
    )
    t2 = time.perf_counter()
    reg.add(corrected)
    corrected_pins = dict(pins)
    corrected_pins[edge0] = "HQ0Q1-v2"
    re_traced = 0
    for rid, season, ivs in reports[:50]:
        tr = tracer.trace_forward(season, ivs[0], last_season, corrected_pins)
        re_traced += len(tr.fragments)
    correction_secs = time.perf_counter() - t2
    print("\n[3] erratum on the first edge, re-followed across all later seasons")
    print(f"    {correction_secs*1000:.1f} ms to publish + re-trace 50 reports "
          f"({re_traced} surviving fragments)")
    print("    old version HQ0Q1-v1 stays immutable and replayable.")

    print("\nNote: trace cost is O(edges x log pairs + clipped pieces); the "
          "sorted pair index avoids scanning whole axes, and every boundary "
          "stays an exact Fraction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
