"""Repeatable end-to-end demonstration.

Part A reproduces the worked example from the task statement exactly:
old report [0,8), handover old[0,3)->new[0,3) / old[5,8)->new[6,9), a new
middle section that merely reuses an old label, contract new[0,8) at price 2:

    no prior payments                 -> 10
    ground [6,7) already paid         ->  8
    corrected book (second pair ->5,8)-> 12

Part B is a three-season walk (Q1 -> Q2 -> Q3) that exercises:

* half-open endpoints (zones ending on pair and section boundaries);
* repeated labels (label 2 on two sections in one season, plus label 5
  reused on different physical ground in another season) -- labels are always
  resolved through the bound section/base of the report's own season;
* per-step loss accounting over chained handovers;
* same-batch allocation in explicit report order (overlap paid once, to the
  first report); newly built ground never paid;
* signed-batch replay that re-explains every line offline;
* a handover correction that debits/credits per voucher, and a second
  correction whose extra pay is *blocked* because another confirmed voucher
  already holds that ground (no double payment, even via an erratum).

Everything is built in memory and also written under ``--out DIR`` as JSON
snapshots so the evidence survives the process.  Run:

    python3 solution/run_cross_season.py
    python3 solution/run_cross_season.py --out /tmp/evidence
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from fractions import Fraction

from chainage_catalog import Interval, RouteCatalog, Section
from handover_book import HandoverPair, HandoverRegistry, HandoverVersion
from ground_lineage import LineageTracer
from maintenance_ledger import (
    Contract,
    ContractZone,
    CorrectionAdvice,
    PaymentLedger,
    SettlementBatch,
    SettlementEngine,
    VoucherLine,
    advise_correction,
)


def frac_str(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else (
        f"{value.numerator}/{value.denominator}"
    )


def iv_str(iv: Interval) -> str:
    return f"[{frac_str(iv.start)}, {frac_str(iv.end)})"


def check(label: str, got, expected) -> None:
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"  [{status}] {label}: got "
          f"{frac_str(got) if isinstance(got, Fraction) else got}, expected "
          f"{frac_str(expected) if isinstance(expected, Fraction) else expected}")
    if not ok:
        raise AssertionError(label)


# ===========================================================================
# Part A: the task's canonical worked example
# ===========================================================================


def part_a(out_dir: str | None) -> None:
    print("=" * 78)
    print("Part A.  Worked example (old [0,8); contract new [0,8) price 2)")
    print("=" * 78)

    reg = HandoverRegistry()
    h1 = HandoverVersion(
        "H-v1", "S0", "S1", Fraction(8), Fraction(9),
        (
            HandoverPair(Fraction(0), Fraction(3), Fraction(0), Fraction(3)),
            HandoverPair(Fraction(5), Fraction(8), Fraction(6), Fraction(9)),
        ),
    )
    reg.add(h1)
    tracer = LineageTracer(reg)
    engine = SettlementEngine(reg, tracer)
    contract = Contract("S1", (ContractZone("Z", Fraction(0), Fraction(8), Fraction(2)),))
    report = [Interval(Fraction(0), Fraction(8))]
    pins_v1 = {("S0", "S1"): "H-v1"}

    print("\nCatalog trace of old span [0,8) under H-v1:")
    trace = tracer.trace_forward("S0", Interval(Fraction(0), Fraction(8)), "S1", pins_v1)
    for frag in trace.fragments:
        print(f"  survives -> new {iv_str(frag.interval)} (born {frag.birth_season}"
              f" {iv_str(frag.birth_interval)})")
    for edge, vid, lost in trace.loss_by_step():
        print(f"  lost at {edge[0]}->{edge[1]} via {vid}: length {frac_str(lost)}")
    check("surviving length", trace.surviving_length, Fraction(6))
    check("lost length", trace.total_lost_length, Fraction(2))
    print("  note: new [3,6) is new-build ground even though it carries an old label;")
    print("        its ancestry query reports birth season S1, so it is never paid.")

    # Case 1: empty prior ledger.
    batch1, ledger1 = engine.settle_batch(
        "A-B1", "S1", [("R-old", "S0", report)],
        contract, PaymentLedger(), pins_v1,
    )
    print(f"\nCase 1 (no prior payments): voucher {batch1.total_amount}")
    _print_lines(batch1.lines)
    check("case 1 amount", batch1.total_amount, Fraction(10))

    # Case 2: ground [6,7) on the new axis has already been paid.
    prior = PaymentLedger([
        VoucherLine(
            voucher_id="PRIOR", report_id="PRIOR-REPORT", target_season="S1",
            target_interval=Interval(Fraction(6), Fraction(7)),
            birth_season="S0", birth_interval=Interval(Fraction(5), Fraction(6)),
            zone_id="Z", length=Fraction(1), unit_price=Fraction(2),
            amount=Fraction(2), handover_versions=("H-v1",),
        )
    ])
    batch2, _ = engine.settle_batch(
        "A-B2", "S1", [("R-old", "S0", report)], contract, prior, pins_v1,
    )
    print(f"\nCase 2 ([6,7) already paid): voucher {batch2.total_amount}")
    _print_lines(batch2.lines)
    check("case 2 amount", batch2.total_amount, Fraction(8))

    # Case 3: corrected book -- second pair now old[5,8) -> new[5,8).
    h2 = HandoverVersion(
        "H-v2", "S0", "S1", Fraction(8), Fraction(9),
        (
            HandoverPair(Fraction(0), Fraction(3), Fraction(0), Fraction(3)),
            HandoverPair(Fraction(5), Fraction(8), Fraction(5), Fraction(8)),
        ),
        supersedes="H-v1",
    )
    reg.add(h2)
    pins_v2 = {("S0", "S1"): "H-v2"}
    batch3, _ = engine.settle_batch(
        "A-B3", "S1", [("R-old", "S0", report)],
        contract, PaymentLedger(), pins_v2,
    )
    print(f"\nCase 3 (corrected H-v2, empty ledger): voucher {batch3.total_amount}")
    _print_lines(batch3.lines)
    check("case 3 amount", batch3.total_amount, Fraction(12))

    advice = advise_correction(
        engine, batch1, {"R-old": ("S0", report)}, PaymentLedger(), pins_v2
    )
    print("\nCorrection advice against the signed A-B1 (voucher unchanged until confirmed):")
    for a in advice.per_voucher:
        print(f"  {a.kind:7s} {a.voucher_id}: target {iv_str(a.target_interval)} "
              f"length {frac_str(a.length)} amount {frac_str(a.amount)} -- {a.reason}")
    print(f"  net cash movement: {frac_str(advice.net_cash)}")
    check("advice net", advice.net_cash, Fraction(2))

    if out_dir:
        batch1.save_json(os.path.join(out_dir, "partA_batch_v1.json"))
        batch3.save_json(os.path.join(out_dir, "partA_batch_v2.json"))
        advice.save_json(os.path.join(out_dir, "partA_advice.json"))
        reg.save_json(os.path.join(out_dir, "partA_handovers.json"))


# ===========================================================================
# Part B: three seasons, labels, endpoint/rational zones, replay, blocking
# ===========================================================================


def _catalogs() -> dict[str, RouteCatalog]:
    # Q1 axis [0,12): two sections, labels cover each section once.
    q1 = RouteCatalog("Q1", "TRAIL", Fraction(12), (
        Section("Q1-A", Fraction(0), Fraction(6), Fraction(0)),
        Section("Q1-B", Fraction(6), Fraction(12), Fraction(10)),
    ))
    # Q2 axis [0,12): repeated label 2 (Q2-A@2 and Q2-C@2), gap labels possible.
    q2 = RouteCatalog("Q2", "TRAIL", Fraction(12), (
        Section("Q2-A", Fraction(0), Fraction(5), Fraction(0)),    # labels 0..5
        Section("Q2-B", Fraction(5), Fraction(8), Fraction(10)),   # labels 10..13
        Section("Q2-C", Fraction(8), Fraction(12), Fraction(0)),   # labels 0..4 (dup of A)
    ))
    # Q3 axis [0,13): label 5 reused on brand-new ground Q3-N.
    q3 = RouteCatalog("Q3", "TRAIL", Fraction(13), (
        Section("Q3-A", Fraction(0), Fraction(4), Fraction(0)),    # labels 0..4
        Section("Q3-B", Fraction(4), Fraction(9), Fraction(10)),   # labels 10..15
        Section("Q3-N", Fraction(9), Fraction(13), Fraction(5)),   # labels 5..9 (reused)
    ))
    return {"Q1": q1, "Q2": q2, "Q3": q3}


def _handovers() -> HandoverRegistry:
    reg = HandoverRegistry()
    # Q1 -> Q2, version 1: old [4,6) and [10,12) retired; new [5,8), [11,12) built.
    reg.add(HandoverVersion("H12-v1", "Q1", "Q2", Fraction(12), Fraction(12), (
        HandoverPair(Fraction(0), Fraction(4), Fraction(0), Fraction(4)),
        HandoverPair(Fraction(6), Fraction(10), Fraction(8), Fraction(12)),
    )))
    # Correction: old [4,6) was actually retained, and the old [6,10) pairing
    # is re-stated at a shifted new position.  R1 keeps target [0,4) but the
    # fate of old [3,4) changes (retired -> retained as new [3,4)), and R2's
    # old [6,8) moves from new [8,10) to new [7,9), so every report's target
    # ground genuinely moves and per-voucher advice is non-trivial.
    reg.add(HandoverVersion("H12-v2", "Q1", "Q2", Fraction(12), Fraction(12), (
        HandoverPair(Fraction(0), Fraction(4), Fraction(0), Fraction(4)),
        HandoverPair(Fraction(4), Fraction(6), Fraction(4), Fraction(6)),
        HandoverPair(Fraction(6), Fraction(10), Fraction(6), Fraction(10)),
    ), supersedes="H12-v1"))
    # Q2 -> Q3, version 1 (Q3 axis length 13).
    reg.add(HandoverVersion("H23-v1", "Q2", "Q3", Fraction(12), Fraction(13), (
        HandoverPair(Fraction(0), Fraction(5), Fraction(0), Fraction(5)),
        HandoverPair(Fraction(8), Fraction(11), Fraction(5), Fraction(8)),
    )))
    # Correction: Q2 [5,8) and [11,12) were mis-filed as new build / retired.
    reg.add(HandoverVersion("H23-v2", "Q2", "Q3", Fraction(12), Fraction(13), (
        HandoverPair(Fraction(0), Fraction(5), Fraction(0), Fraction(5)),
        HandoverPair(Fraction(5), Fraction(8), Fraction(8), Fraction(11)),
        HandoverPair(Fraction(8), Fraction(11), Fraction(5), Fraction(8)),
        HandoverPair(Fraction(11), Fraction(12), Fraction(11), Fraction(12)),
    ), supersedes="H23-v1"))
    return reg


def part_b(out_dir: str | None) -> None:
    print("\n" + "=" * 78)
    print("Part B.  Three seasons Q1 -> Q2 -> Q3 (endpoints, dup labels, replay)")
    print("=" * 78)

    catalogs = _catalogs()
    handovers = _handovers()
    tracer = LineageTracer(handovers)
    engine = SettlementEngine(handovers, tracer)
    pins_v1 = {("Q1", "Q2"): "H12-v1", ("Q2", "Q3"): "H23-v1"}

    # -- labels are resolved via the report's own bound section/base ------
    print("\nRepeated-label resolution (never a global coordinate):")
    r = catalogs["Q2"].resolve_label_point(Fraction(2))
    print(f"  Q2 label 2: {r['status']} -> "
          + ", ".join(f"{c['section_id']}@{frac_str(c['physical'])}"
                      for c in r["candidates"]))
    assert r["status"] == "ambiguous" and len(r["candidates"]) == 2
    r = catalogs["Q2"].resolve_label_point(Fraction(2), section_id="Q2-C")
    print(f"  Q2 label 2 with section Q2-C: {r['status']} -> "
          f"{r['candidates'][0]['section_id']}@{frac_str(r['candidates'][0]['physical'])}")
    r = catalogs["Q3"].resolve_label_point(Fraction(5), section_id="Q3-N")
    print(f"  Q3 label 5 on Q3-N -> physical {frac_str(r['candidates'][0]['physical'])}"
          " (new-build ground: same label as old ground proves nothing)")

    # -- reports: label spans converted on each season's own catalog ------
    def physical(report_season: str, spans: list[tuple[str, int, int]]) -> list[Interval]:
        cat = catalogs[report_season]
        ivs = [cat.convert_span(sid, lo, hi)["physical"] for sid, lo, hi in spans]
        return ivs

    # Q1 reports.
    r1 = physical("Q1", [("Q1-A", 0, 6)])                       # phys [0,6)
    r2 = physical("Q1", [("Q1-A", 4, 6), ("Q1-B", 10, 12)])     # phys [4,6)+[10,12)
    # Q2 report: the repeated label 2 disambiguated by binding section Q2-C.
    r3 = physical("Q2", [("Q2-C", 2, 4)])                       # phys [10,12)
    # Q3 native reports.
    r4 = physical("Q3", [("Q3-A", 0, 4)])                       # phys [0,4)
    r5 = physical("Q3", [("Q3-N", 5, 9)])                       # phys [9,13): new build

    report_index = {
        "R1": ("Q1", r1), "R2": ("Q1", r2), "R3": ("Q2", r3),
        "R4": ("Q3", r4), "R5": ("Q3", r5),
    }

    # -- catalog-client lineage query with per-step loss ------------------
    print("\nLineage query: old Q1 span [0,12) -> Q3 under v1 pins")
    tr = tracer.trace_forward("Q1", Interval(Fraction(0), Fraction(12)), "Q3", pins_v1)
    for frag in tr.fragments:
        print(f"  Q3 {iv_str(frag.interval)}  born {frag.birth_season} "
              f"{iv_str(frag.birth_interval)}")
    for edge, vid, lost in tr.loss_by_step():
        print(f"  step {edge[0]}->{edge[1]} ({vid}) lost length {frac_str(lost)}")
    check("Q1->Q3 surviving", tr.surviving_length, Fraction(7))
    check("Q1->Q3 total lost", tr.total_lost_length, Fraction(5))

    print("\nNo back-dating: Q3 [9,13) ancestry under v1 pins")
    inherited, new_built = tracer.trace_back_origin("Q3", Interval(Fraction(9), Fraction(13)),
                                                    pins=pins_v1)
    print("  inherited:", [iv_str(f.interval) for f in inherited] or "(none)")
    print("  new build:", [iv_str(f.interval) for f in new_built])
    assert not inherited and len(new_built) == 1

    # -- contracts: rational endpoint exercises exact clipping ------------
    # Three priced bands with two boundaries: a fractional one at 5/2 and an
    # integer price change at 3, so a correction moving ground across 3 has a
    # real, non-zero 应补/应减 cash effect.
    contract_q2 = Contract("Q2", (
        ContractZone("ZA", Fraction(0), Fraction(5, 2), Fraction(2)),   # [0,2.5)
        ContractZone("ZB", Fraction(5, 2), Fraction(3), Fraction(1)),   # [2.5,3)
        ContractZone("ZC", Fraction(3), Fraction(12), Fraction(1, 2)),  # [3,12)
    ))
    contract_q3 = Contract("Q3", (
        ContractZone("ZC", Fraction(0), Fraction(13), Fraction(5)),
    ))

    # -- Batch B1: target Q2, explicit order R1 then R2 -------------------
    print("\nBatch B1 -> Q2 (pins H12-v1), order R1 then R2")
    b1, ledger_after_b1 = engine.settle_batch(
        "B1", "Q2",
        [("R1", "Q1", r1), ("R2", "Q1", r2)],
        contract_q2, PaymentLedger(), pins_v1,
    )
    _print_lines(b1.lines)
    print(f"  batch total: {frac_str(b1.total_amount)}")
    check("B1 total", b1.total_amount, Fraction(7))

    # -- Batch B2: target Q3, R1, R3, R4, R5 in explicit order ------------
    print("\nBatch B2 -> Q3 (pins H12-v1/H23-v1), order R1, R3, R4, R5")
    b2, ledger_after_b2 = engine.settle_batch(
        "B2", "Q3",
        [("R1", "Q1", r1), ("R3", "Q2", r3), ("R4", "Q3", r4), ("R5", "Q3", r5)],
        contract_q3, ledger_after_b1, pins_v1,
    )
    _print_lines(b2.lines)
    print(f"  batch total: {frac_str(b2.total_amount)}")

    # -- offline replay of both signed batches ----------------------------
    print("\nOffline replay from frozen batch evidence:")
    ok1, diffs1 = engine.replay_batch(b1, report_index, PaymentLedger())
    ok2, diffs2 = engine.replay_batch(b2, report_index, ledger_after_b1)
    print(f"  B1 replay identical: {ok1} {diffs1}")
    print(f"  B2 replay identical: {ok2} {diffs2}")
    assert ok1 and ok2

    # -- correction 1: H12-v1 -> H12-v2, per-voucher debit/credit ---------
    # R1 (source Q1 [0,6)) legitimately reaches new-axis [0,6) under the
    # correction, so it earns the newly retained [4,6).  R2's ground moves
    # within the same price band: its debit and credit cancel, leaving a real
    # non-zero per-voucher trail and a net +1 cash movement.
    pins_c1 = {("Q1", "Q2"): "H12-v2", ("Q2", "Q3"): "H23-v1"}
    print("\nCorrection 1 advice (H12-v2), old vouchers untouched:")
    adv1 = advise_correction(
        engine, b1, report_index, PaymentLedger(), pins_c1,
        confirmed_ledger=ledger_after_b1,
    )
    _print_advice(adv1)
    check("correction1 net cash", adv1.net_cash, Fraction(1))
    kinds = sorted(a.kind for a in adv1.per_voucher)
    assert "credit" in kinds and "debit" in kinds

    # -- correction 2: H23-v1 -> H23-v2 demonstrates the double-pay block --
    # A native Q3 report over [11,12).  Under H23-v1 this axis part is newly
    # built ground, so it is not payable at all; under H23-v2 the corrected
    # book recognises it as inherited (birth Q1 [9,10)), so a re-simulation
    # wants to credit it.  An external confirmed voucher already paid that
    # exact birth ground, so the credit is blocked: the same physical ground
    # is never paid twice, even when an erratum re-routes it to this report.
    r_native = [Interval(Fraction(11), Fraction(12))]
    pins_v1_local = {("Q1", "Q2"): "H12-v1", ("Q2", "Q3"): "H23-v1"}
    pins_c2 = {("Q1", "Q2"): "H12-v1", ("Q2", "Q3"): "H23-v2"}
    external_holder = PaymentLedger([
        VoucherLine(
            voucher_id="EXT-V0", report_id="EXT-REPORT", target_season="Q3",
            target_interval=Interval(Fraction(11), Fraction(12)),
            birth_season="Q1", birth_interval=Interval(Fraction(9), Fraction(10)),
            zone_id="ZC", length=Fraction(1), unit_price=Fraction(5),
            amount=Fraction(5), handover_versions=("H23-v2",),
        )
    ])
    block_batch, _ = engine.settle_batch(
        "BLK", "Q3", [("RN", "Q3", r_native)],
        contract_q3, PaymentLedger(), pins_v1_local,
    )
    assert block_batch.total_amount == 0  # new build: nothing payable under v1
    print("\nCorrection 2 advice (H23-v2): extra ground already held elsewhere is blocked")
    adv2 = advise_correction(
        engine, block_batch, {"RN": ("Q3", r_native)}, PaymentLedger(),
        pins_c2, confirmed_ledger=external_holder,
    )
    _print_advice(adv2)
    blocked = [a for a in adv2.per_voucher if a.kind == "blocked"]
    assert blocked, "expected at least one blocked (no-double-pay) advice"
    check("correction2 blocked cash stays unpaid", adv2.blocked_amount,
          sum(a.amount for a in blocked))
    check("correction2 net cash is zero (block, not pay)", adv2.net_cash, Fraction(0))
    print(f"  blocked amount never paid again: {frac_str(adv2.blocked_amount)}")

    if out_dir:
        b1.save_json(os.path.join(out_dir, "partB_B1_signed.json"))
        b2.save_json(os.path.join(out_dir, "partB_B2_signed.json"))
        adv1.save_json(os.path.join(out_dir, "partB_advice_h12v2.json"))
        adv2.save_json(os.path.join(out_dir, "partB_advice_h23v2.json"))
        handovers.save_json(os.path.join(out_dir, "partB_handovers.json"))
        ledger_after_b2.save_json(os.path.join(out_dir, "partB_ledger.json"))
        # Round-trip proof: reload and replay.
        reloaded = SettlementBatch.load_json(os.path.join(out_dir, "partB_B2_signed.json"))
        ok, diffs = engine.replay_batch(reloaded, report_index, ledger_after_b1)
        print(f"\nReloaded B2 from JSON, replay identical: {ok} {diffs}")
        assert ok


def _print_lines(lines) -> None:
    for ln in lines:
        print(f"  {ln.voucher_id} [{ln.report_id}] pay target {ln.target_season}"
              f" {iv_str(ln.target_interval)} (born {ln.birth_season}"
              f" {iv_str(ln.birth_interval)}) zone {ln.zone_id} "
              f"len {frac_str(ln.length)} x {frac_str(ln.unit_price)} "
              f"= {frac_str(ln.amount)}  pins={list(ln.handover_versions)}")


def _print_advice(advice: CorrectionAdvice) -> None:
    for a in advice.per_voucher:
        print(f"  {a.kind:7s} {a.voucher_id} [{a.report_id}] target "
              f"{iv_str(a.target_interval)} len {frac_str(a.length)} "
              f"amount {frac_str(a.amount)} -- {a.reason}")
    print(f"  net cash: {frac_str(advice.net_cash)}; "
          f"blocked (not paid): {frac_str(advice.blocked_amount)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="directory for JSON evidence")
    args = parser.parse_args(argv)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    part_a(args.out)
    part_b(args.out)
    print("\nAll assertions passed; evidence directory: "
          f"{args.out or '(in-memory only)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
