"""Maintenance settlement: pay inherited ground, exactly once, explainably.

The maintenance side holds, per target season, contract zones with unit
prices and the vouchers already issued.  Money follows physical ground, never
labels or mileage, so every payment is resolved through the pinned handover
chain to a *birth identity* (``birth_season`` + half-open interval on the
birth axis).  Two fragments overlap on the same ground iff their birth
identities' intervals intersect.

Payment rules implemented here
------------------------------
* A report is mapped onto the target season with the explicitly pinned
  handover versions, then clipped to the published contract zones.
* Only **inherited** ground is payable.  Ground born on the target season's
  axis is the new-build part of the book: the scheme only pays for ground
  carried forward, so it is excluded even if a report covers it.
* The union of already-paid birth intervals is deducted first; within one
  settlement batch reports are allocated in the *explicit order* given, so
  the first report in the list owns every contested overlap.
* Each voucher line records the target ground actually paid, its birth
  ancestry (source), the report that earned it and the exact handover
  versions used.  Re-reading a signed batch replays the same inputs and
  reproduces the same lines byte-for-byte.
* Corrections never mutate old vouchers.  A correction is re-simulated into
  per-voucher ``credit`` / ``debit`` advice; a simulated add whose ground is
  already held by another confirmed voucher is reported as ``blocked``
  (never pay the same ground twice), and the net cash effect is stated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from chainage_catalog import Interval, as_fraction, fraction_to_db, merge_intervals
from ground_lineage import GroundFragment, LineageTracer, _merge_fragments
from handover_book import HandoverRegistry, clip, subtract


class SettlementError(ValueError):
    """Base error for settlement processing."""


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractZone:
    """One priced half-open zone on a target season's axis."""

    zone_id: str
    start: Fraction
    end: Fraction
    unit_price: Fraction

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)

    def __post_init__(self):
        object.__setattr__(self, "start", as_fraction(self.start))
        object.__setattr__(self, "end", as_fraction(self.end))
        object.__setattr__(self, "unit_price", as_fraction(self.unit_price))
        if not self.start < self.end:
            raise SettlementError(f"zone {self.zone_id!r} must have positive length")
        if self.unit_price < 0:
            raise SettlementError(f"zone {self.zone_id!r} has a negative price")


@dataclass(frozen=True)
class Contract:
    """The priced zones in force for one target season."""

    season: str
    zones: tuple[ContractZone, ...]

    def __post_init__(self):
        ordered = sorted(self.zones, key=lambda z: (z.start, z.end))
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.start < prev.end:
                raise SettlementError(
                    f"contract zones overlap: {prev.zone_id} and {nxt.zone_id}"
                )

    def clip(self, interval: Interval) -> list[tuple[ContractZone, Interval]]:
        """Return ``(zone, clipped piece)`` for every priced overlap."""
        out: list[tuple[ContractZone, Interval]] = []
        for zone in sorted(self.zones, key=lambda z: z.start):
            piece = clip(interval, zone.interval)
            if piece is not None:
                out.append((zone, piece))
        return out

    def covered_axis(self) -> list[Interval]:
        return merge_intervals(z.interval for z in self.zones)


# ---------------------------------------------------------------------------
# Vouchers and the once-only payment ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoucherLine:
    """One paid fragment within a voucher (a voucher is one report's result)."""

    voucher_id: str
    report_id: str
    target_season: str
    target_interval: Interval
    birth_season: str
    birth_interval: Interval
    zone_id: str
    length: Fraction
    unit_price: Fraction
    amount: Fraction
    handover_versions: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "voucher_id": self.voucher_id,
            "report_id": self.report_id,
            "target_season": self.target_season,
            "target_start": fraction_to_db(self.target_interval.start),
            "target_end": fraction_to_db(self.target_interval.end),
            "birth_season": self.birth_season,
            "birth_start": fraction_to_db(self.birth_interval.start),
            "birth_end": fraction_to_db(self.birth_interval.end),
            "zone_id": self.zone_id,
            "length": fraction_to_db(self.length),
            "unit_price": fraction_to_db(self.unit_price),
            "amount": fraction_to_db(self.amount),
            "handover_versions": list(self.handover_versions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VoucherLine":
        return cls(
            voucher_id=data["voucher_id"],
            report_id=data["report_id"],
            target_season=data["target_season"],
            target_interval=Interval(as_fraction(data["target_start"]),
                                     as_fraction(data["target_end"])),
            birth_season=data["birth_season"],
            birth_interval=Interval(as_fraction(data["birth_start"]),
                                    as_fraction(data["birth_end"])),
            zone_id=data["zone_id"],
            length=as_fraction(data["length"]),
            unit_price=as_fraction(data["unit_price"]),
            amount=as_fraction(data["amount"]),
            handover_versions=tuple(data["handover_versions"]),
        )


class PaymentLedger:
    """All confirmed vouchers.  Ownership is keyed by birth identity."""

    def __init__(self, lines: Optional[Iterable[VoucherLine]] = None):
        self._lines: list[VoucherLine] = list(lines or [])
        self._paid_cache: Optional[list[Interval]] = None

    @property
    def lines(self) -> list[VoucherLine]:
        return list(self._lines)

    def paid_birth_intervals(self) -> list[Interval]:
        # Cached union: settlement makes many residual queries against an
        # only-occasionally-growing ledger, so recompute only after adds.
        if self._paid_cache is None:
            self._paid_cache = merge_intervals(
                line.birth_interval for line in self._lines
            )
        return self._paid_cache

    def residual(self, fragment: GroundFragment) -> list[Interval]:
        """Piece of a fragment's birth interval not yet paid to anyone."""
        return subtract(fragment.birth_interval, self.paid_birth_intervals())

    def add(self, lines: Iterable[VoucherLine]) -> None:
        new = list(lines)
        if new:
            self._lines.extend(new)
            self._paid_cache = None  # invalidate the cached union

    def for_report(self, report_id: str) -> list[VoucherLine]:
        return [ln for ln in self._lines if ln.report_id == report_id]

    def total_amount(self) -> Fraction:
        return sum((ln.amount for ln in self._lines), Fraction(0))

    def to_dict(self) -> dict:
        return {"lines": [ln.to_dict() for ln in self._lines]}

    @classmethod
    def from_dict(cls, data: dict) -> "PaymentLedger":
        return cls(VoucherLine.from_dict(d) for d in data["lines"])

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "PaymentLedger":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------------------
# Signed settlement batch: frozen evidence, replayable offline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementBatch:
    batch_id: str
    target_season: str
    report_order: tuple[str, ...]
    pins: dict  # edge -> version_id (JSON-safe keys are [from, to])
    contract: Contract
    lines: tuple[VoucherLine, ...]
    total_amount: Fraction

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "target_season": self.target_season,
            "report_order": list(self.report_order),
            "pins": {f"{edge[0]}>{edge[1]}": vid for edge, vid in self.pins.items()},
            "contract": {
                "season": self.contract.season,
                "zones": [
                    {
                        "zone_id": z.zone_id,
                        "start": fraction_to_db(z.start),
                        "end": fraction_to_db(z.end),
                        "unit_price": fraction_to_db(z.unit_price),
                    }
                    for z in self.contract.zones
                ],
            },
            "lines": [ln.to_dict() for ln in self.lines],
            "total_amount": fraction_to_db(self.total_amount),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SettlementBatch":
        pins = {
            tuple(key.split(">", 1)): vid for key, vid in data["pins"].items()
        }
        zones = tuple(
            ContractZone(
                zone_id=z["zone_id"],
                start=as_fraction(z["start"]),
                end=as_fraction(z["end"]),
                unit_price=as_fraction(z["unit_price"]),
            )
            for z in data["contract"]["zones"]
        )
        return cls(
            batch_id=data["batch_id"],
            target_season=data["target_season"],
            report_order=tuple(data["report_order"]),
            pins=pins,
            contract=Contract(season=data["contract"]["season"], zones=zones),
            lines=tuple(VoucherLine.from_dict(d) for d in data["lines"]),
            total_amount=as_fraction(data["total_amount"]),
        )

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "SettlementBatch":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------------------
# Settlement engine
# ---------------------------------------------------------------------------


class SettlementEngine:
    def __init__(self, handovers: HandoverRegistry, tracer: LineageTracer):
        self.handovers = handovers
        self.tracer = tracer

    # -- ancestry of a target-axis interval under pins ----------------
    def resolve_ancestry(
        self,
        target_season: str,
        interval: Interval,
        pins: dict,
    ) -> tuple[list[GroundFragment], list[GroundFragment]]:
        """Return (inherited, new-built) fragments with birth identities."""
        return self.tracer.trace_back_origin(
            target_season, interval, pins=pins,
            oldest_season=self.handovers.seasons[0],
        )

    def _pinned_versions(self, target_season: str, pins: dict) -> tuple[str, ...]:
        used = []
        for edge in self.handovers.edge_path(target_season,
                                             self.handovers.seasons[0]):
            book = self.handovers.resolve((edge[1], edge[0]), pins)
            used.append(book.version_id)
        return tuple(used)

    def report_target_coverage(
        self,
        source_season: str,
        source_intervals: Sequence[Interval],
        target_season: str,
        pins: dict,
    ) -> list[Interval]:
        """The exact target-axis ground a report reaches under these pins."""
        if source_season == target_season:
            return merge_intervals(source_intervals)
        out: list[Interval] = []
        for siv in merge_intervals(source_intervals):
            trace = self.tracer.trace_forward(
                source_season, siv, target_season, pins
            )
            out.extend(f.interval for f in trace.fragments)
        return merge_intervals(out)

    # -- one report against one ledger snapshot -----------------------
    def settle_report(
        self,
        report_id: str,
        voucher_id: str,
        source_season: str,
        source_intervals: Sequence[Interval],
        target_season: str,
        contract: Contract,
        ledger: PaymentLedger,
        pins: dict,
    ) -> list[VoucherLine]:
        if contract.season != target_season:
            raise SettlementError("contract season does not match target season")
        # 1. Map every report interval onto the target axis (union; overlaps
        #    never double count).  Each interval is traced independently so a
        #    multi-interval report needs no bounding-span shortcut.
        target_intervals: list[Interval] = []
        if source_season == target_season:
            target_intervals = merge_intervals(source_intervals)
        else:
            for siv in merge_intervals(source_intervals):
                trace = self.tracer.trace_forward(
                    source_season, siv, target_season, pins
                )
                target_intervals.extend(f.interval for f in trace.fragments)
            target_intervals = merge_intervals(target_intervals)
        pinned_versions = self._pinned_versions(target_season, pins)

        # Forward-trace every report interval to the target axis.  Cross-
        # season fragments already carry their exact birth intervals.  For a
        # same-season report a backward ancestry walk separates inherited
        # ground (payable) from ground newly built on this axis (never paid,
        # however a label is reused).
        if source_season == target_season:
            # Keep the target-axis interval (what the report physically
            # covers) and resolve its birth identity by walking the *target*
            # interval backward through the pinned books.  The ancestry
            # fragment's own interval lives on the birth axis, so it is never
            # clipped against target-axis coordinates.
            fragments: list[GroundFragment] = []
            for iv in target_intervals:
                inherited, _new_built = self.resolve_ancestry(
                    target_season, iv, pins
                )
                for frag in inherited:
                    target_piece = clip(iv, iv)
                    birth_piece = self._rebase_target_to_birth(
                        target_season, target_piece, frag.birth_season, pins
                    )
                    fragments.append(
                        GroundFragment(
                            season=target_season, interval=target_piece,
                            birth_season=frag.birth_season,
                            birth_interval=birth_piece,
                        )
                    )
        else:
            fragments = []
            for siv in merge_intervals(source_intervals):
                trace = self.tracer.trace_forward(
                    source_season, siv, target_season, pins
                )
                fragments.extend(trace.fragments)

        lines: list[VoucherLine] = []
        report_union = merge_intervals(target_intervals)
        for frag in _merge_fragments(fragments):
            # A birth fragment can extend beyond what this report still
            # covers on the target axis; the report only owns the overlap.
            for covered in report_union:
                owned = clip(frag.interval, covered)
                if owned is None:
                    continue
                for zone, priced_piece in contract.clip(owned):
                    # Birth identity follows the same slope-+1 affine map:
                    # the offset between a fragment's target interval and its
                    # birth interval is constant, so no extra backward walk
                    # (which would fail on a piece spanning pair boundaries).
                    offset = frag.birth_interval.start - frag.interval.start
                    birth_piece = Interval(
                        priced_piece.start + offset,
                        priced_piece.end + offset,
                    )
                    # Once-only in birth coordinates against every confirmed line.
                    for unborn in ledger.residual(
                        GroundFragment(
                            season=target_season, interval=priced_piece,
                            birth_season=frag.birth_season, birth_interval=birth_piece,
                        )
                    ):
                        target_piece = self.tracer.birth_piece_at_target(
                            frag.birth_season, unborn, target_season, pins
                        )
                        target_piece = clip(target_piece, priced_piece)
                        if target_piece is None:
                            continue
                        lines.append(
                            VoucherLine(
                                voucher_id=voucher_id,
                                report_id=report_id,
                                target_season=target_season,
                                target_interval=target_piece,
                                birth_season=frag.birth_season,
                                birth_interval=unborn,
                                zone_id=zone.zone_id,
                                length=target_piece.length,
                                unit_price=zone.unit_price,
                                amount=target_piece.length * zone.unit_price,
                                handover_versions=pinned_versions,
                            )
                        )
        return _coalesce_lines(lines)

    def _rebase_target_to_birth(
        self, target_season: str, target_piece: Interval,
        birth_season: str, pins: dict,
    ) -> Interval:
        """Affine-walk a target-axis interval back onto a birth axis."""
        if birth_season == target_season:
            return target_piece
        work = target_piece
        for edge in self.handovers.edge_path(target_season, birth_season):
            book = self.handovers.resolve((edge[1], edge[0]), pins)
            mapped, new_build = book.map_backward(work)
            if new_build:
                raise SettlementError(
                    "internal: target piece resolves to new build while rebasing"
                )
            if len(mapped) != 1:
                raise SettlementError("internal: target piece splits while rebasing")
            work = mapped[0]
        return work

    # -- an ordered batch: the allocation order is part of the rule ---
    def settle_batch(
        self,
        batch_id: str,
        target_season: str,
        ordered_reports: Sequence[tuple[str, str, Sequence[Interval]]],
        contract: Contract,
        prior_ledger: PaymentLedger,
        pins: dict,
    ) -> tuple[SettlementBatch, PaymentLedger]:
        """Each tuple is ``(report_id, source_season, source_intervals)``.

        Allocation follows the given report order explicitly: a report only
        receives ground left over after every earlier report (and the prior
        confirmed ledger) has been satisfied.
        """
        ledger = PaymentLedger(prior_ledger.lines)  # work on a copy
        all_lines: list[VoucherLine] = []
        for idx, (report_id, source_season, intervals) in enumerate(ordered_reports):
            lines = self.settle_report(
                report_id=report_id,
                voucher_id=f"{batch_id}.V{idx:02d}",
                source_season=source_season,
                source_intervals=intervals,
                target_season=target_season,
                contract=contract,
                ledger=ledger,
                pins=dict(pins),
            )
            ledger.add(lines)
            all_lines.extend(lines)
        batch = SettlementBatch(
            batch_id=batch_id,
            target_season=target_season,
            report_order=tuple(r[0] for r in ordered_reports),
            pins=dict(pins),
            contract=contract,
            lines=tuple(all_lines),
            total_amount=sum((ln.amount for ln in all_lines), Fraction(0)),
        )
        return batch, ledger

    # -- offline replay of a signed batch -----------------------------
    def replay_batch(
        self,
        batch: SettlementBatch,
        report_index: dict,
        prior_ledger: PaymentLedger,
    ) -> tuple[bool, list[str]]:
        """Re-run the batch from its frozen inputs; return (matches, diffs)."""
        ordered = [
            (rid, report_index[rid][0], report_index[rid][1])
            for rid in batch.report_order
        ]
        replayed, _ = self.settle_batch(
            batch_id=batch.batch_id,
            target_season=batch.target_season,
            ordered_reports=ordered,
            contract=batch.contract,
            prior_ledger=prior_ledger,
            pins=dict(batch.pins),
        )
        diffs: list[str] = []
        if [ln.to_dict() for ln in replayed.lines] != [
            ln.to_dict() for ln in batch.lines
        ]:
            diffs.append("voucher lines differ")
        if replayed.total_amount != batch.total_amount:
            diffs.append(
                f"total {replayed.total_amount} != signed {batch.total_amount}"
            )
        return not diffs, diffs


# ---------------------------------------------------------------------------
# Correction advice (old vouchers stay untouched until confirmed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineAdvice:
    voucher_id: str
    report_id: str
    kind: str  # "credit" (应补) | "debit" (应减) | "blocked" (重复拦截)
    target_interval: Interval
    birth_interval: Interval
    zone_id: str
    length: Fraction
    amount: Fraction
    reason: str
    handover_versions: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "voucher_id": self.voucher_id,
            "report_id": self.report_id,
            "kind": self.kind,
            "target_start": fraction_to_db(self.target_interval.start),
            "target_end": fraction_to_db(self.target_interval.end),
            "birth_start": fraction_to_db(self.birth_interval.start),
            "birth_end": fraction_to_db(self.birth_interval.end),
            "zone_id": self.zone_id,
            "length": fraction_to_db(self.length),
            "amount": fraction_to_db(self.amount),
            "reason": self.reason,
            "handover_versions": list(self.handover_versions),
        }


@dataclass(frozen=True)
class CorrectionAdvice:
    batch_id: str
    old_pins: dict
    corrected_pins: dict
    per_voucher: tuple[LineAdvice, ...]
    net_cash: Fraction
    blocked_amount: Fraction

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "old_pins": {f"{e[0]}>{e[1]}": v for e, v in self.old_pins.items()},
            "corrected_pins": {
                f"{e[0]}>{e[1]}": v for e, v in self.corrected_pins.items()
            },
            "per_voucher": [a.to_dict() for a in self.per_voucher],
            "net_cash": fraction_to_db(self.net_cash),
            "blocked_amount": fraction_to_db(self.blocked_amount),
        }

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)


def advise_correction(
    engine: SettlementEngine,
    batch: SettlementBatch,
    report_index: dict,
    prior_ledger: PaymentLedger,
    corrected_pins: dict,
    confirmed_ledger: Optional[PaymentLedger] = None,
) -> CorrectionAdvice:
    """Per-voucher 应补/应减 advice for swapping in a corrected handover.

    The old batch is *not* modified.  The whole batch is re-simulated under
    ``corrected_pins`` with the same explicit report order and the same prior
    ledger, then compared voucher by voucher **on target-axis ground**: a
    target fragment present before and after is the same physical maintenance
    object even though its resolved birth identity changed (that change is
    precisely what the erratum is about).  Any simulated extra ground already
    paid under another confirmed voucher becomes ``blocked`` advice instead of
    a credit: the scheme never pays the same ground twice, even via an erratum.
    """
    corrected_batch, _ = engine.settle_batch(
        batch_id=batch.batch_id,
        target_season=batch.target_season,
        ordered_reports=[
            (rid, report_index[rid][0], report_index[rid][1])
            for rid in batch.report_order
        ],
        contract=batch.contract,
        prior_ledger=prior_ledger,
        pins=dict(corrected_pins),
    )

    # Every corrected voucher line must lie inside the ground its report
    # actually reaches under the corrected books; clip defensively so a
    # re-pairing that widens ancestry can never credit ground the report did
    # not inspect.
    coverage_by_report: dict[str, list[Interval]] = {}
    for rid in batch.report_order:
        src_season, src_ivs = report_index[rid]
        coverage_by_report[rid] = engine.report_target_coverage(
            src_season, src_ivs, batch.target_season, corrected_pins
        )
    clipped_lines: list[VoucherLine] = []
    for ln in corrected_batch.lines:
        for covered in coverage_by_report[ln.report_id]:
            piece = clip(ln.target_interval, covered)
            if piece is None:
                continue
            if piece == ln.target_interval:
                clipped_lines.append(ln)
            else:
                # Reprice the smaller piece at the same zone rate.
                clipped_lines.append(
                    VoucherLine(
                        voucher_id=ln.voucher_id, report_id=ln.report_id,
                        target_season=ln.target_season, target_interval=piece,
                        birth_season=ln.birth_season, birth_interval=ln.birth_interval,
                        zone_id=ln.zone_id, length=piece.length,
                        unit_price=ln.unit_price, amount=piece.length * ln.unit_price,
                        handover_versions=ln.handover_versions,
                    )
                )
    corrected_batch = SettlementBatch(
        batch_id=corrected_batch.batch_id,
        target_season=corrected_batch.target_season,
        report_order=corrected_batch.report_order,
        pins=corrected_batch.pins,
        contract=corrected_batch.contract,
        lines=tuple(_coalesce_lines(clipped_lines)),
        total_amount=sum(
            (l.amount for l in _coalesce_lines(clipped_lines)), Fraction(0)
        ),
    )

    confirmed = confirmed_ledger or PaymentLedger()
    # Confirmed ownership keyed by birth identity, excluding this batch's own
    # vouchers (those are being re-advised, not treated as external holders).
    own_vouchers = {ln.voucher_id for ln in batch.lines}
    external_holds: dict = {}
    for ln in confirmed.lines:
        if ln.voucher_id in own_vouchers:
            continue
        external_holds.setdefault(
            (ln.birth_season, ln.birth_interval.start, ln.birth_interval.end), ln
        )

    advice: list[LineAdvice] = []
    old_by_voucher: dict[str, list[VoucherLine]] = {}
    new_by_voucher: dict[str, list[VoucherLine]] = {}
    for ln in batch.lines:
        old_by_voucher.setdefault(ln.voucher_id, []).append(ln)
    for ln in corrected_batch.lines:
        new_by_voucher.setdefault(ln.voucher_id, []).append(ln)

    def _pieces_not_in(piece_lines: list[VoucherLine],
                       other_lines: list[VoucherLine]) -> list[tuple[VoucherLine, Interval]]:
        """Each (owning line, residual piece) not covered on the target axis."""
        other_union = merge_intervals(ln.target_interval for ln in other_lines)
        out: list[tuple[VoucherLine, Interval]] = []
        for ln in piece_lines:
            for residual in subtract(ln.target_interval, other_union):
                out.append((ln, residual))
        return out

    for vid in sorted(set(old_by_voucher) | set(new_by_voucher)):
        old_lines = old_by_voucher.get(vid, [])
        new_lines = new_by_voucher.get(vid, [])

        # Debits: target ground the voucher used to hold but no longer does.
        for owner, lost_piece in _pieces_not_in(old_lines, new_lines):
            advice.append(LineAdvice(
                voucher_id=vid, report_id=owner.report_id, kind="debit",
                target_interval=lost_piece, birth_interval=owner.birth_interval,
                zone_id=owner.zone_id, length=lost_piece.length,
                amount=lost_piece.length * owner.unit_price,
                reason="corrected handover no longer carries this ground",
                handover_versions=owner.handover_versions,
            ))

        # Credits: newly appearing target ground, unless held elsewhere.
        for owner, gained in _pieces_not_in(new_lines, old_lines):
            holder = external_holds.get(
                (owner.birth_season, owner.birth_interval.start,
                 owner.birth_interval.end)
            )
            if holder is not None and holder.report_id != owner.report_id:
                advice.append(LineAdvice(
                    voucher_id=vid, report_id=owner.report_id, kind="blocked",
                    target_interval=gained, birth_interval=owner.birth_interval,
                    zone_id=owner.zone_id, length=gained.length,
                    amount=gained.length * owner.unit_price,
                    reason=(f"ground already paid via voucher {holder.voucher_id} "
                            f"/ report {holder.report_id}; no double payment"),
                    handover_versions=owner.handover_versions,
                ))
            else:
                advice.append(LineAdvice(
                    voucher_id=vid, report_id=owner.report_id, kind="credit",
                    target_interval=gained, birth_interval=owner.birth_interval,
                    zone_id=owner.zone_id, length=gained.length,
                    amount=gained.length * owner.unit_price,
                    reason="corrected handover restores inherited ground",
                    handover_versions=owner.handover_versions,
                ))

    net = sum(
        (a.amount if a.kind == "credit" else -a.amount if a.kind == "debit"
         else Fraction(0) for a in advice),
        Fraction(0),
    )
    blocked = sum((a.amount for a in advice if a.kind == "blocked"), Fraction(0))
    return CorrectionAdvice(
        batch_id=batch.batch_id,
        old_pins=dict(batch.pins),
        corrected_pins=dict(corrected_pins),
        per_voucher=tuple(advice),
        net_cash=net,
        blocked_amount=blocked,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _coalesce_lines(lines: list[VoucherLine]) -> list[VoucherLine]:
    """Merge adjacent lines that share every payment attribute."""
    grouped: dict = {}
    order: list = []
    for ln in lines:
        key = (
            ln.voucher_id, ln.report_id, ln.target_season, ln.birth_season,
            ln.zone_id, ln.unit_price, ln.handover_versions,
        )
        if key not in grouped:
            grouped[key] = {"birth": [], "target": []}
            order.append(key)
        grouped[key]["birth"].append(ln.birth_interval)
        grouped[key]["target"].append(ln.target_interval)
    out: list[VoucherLine] = []
    for key in order:
        vid, rid, season, birth_season, zone_id, price, versions = key
        birth_ivs = merge_intervals(grouped[key]["birth"])
        target_ivs = merge_intervals(grouped[key]["target"])
        for biv, tiv in zip(birth_ivs, target_ivs):
            out.append(
                VoucherLine(
                    voucher_id=vid,
                    report_id=rid,
                    target_season=season,
                    target_interval=tiv,
                    birth_season=birth_season,
                    birth_interval=biv,
                    zone_id=zone_id,
                    length=tiv.length,
                    unit_price=price,
                    amount=tiv.length * price,
                    handover_versions=versions,
                )
            )
    return out
