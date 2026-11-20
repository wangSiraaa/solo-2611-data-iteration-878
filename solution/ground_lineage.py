"""Ground lineage across measurement seasons.

This module answers the catalog-client question:

    *where did every part of this old span end up by season T, and at which
    handover step did how much length disappear?*

It walks the explicit handover chain one edge at a time.  At every edge each
surviving fragment is clipped against the pinned book version:

* the part inside a pair moves to the other axis (same ground, new mileage);
* the part outside every pair **dies there** -- retired ground if walking
  forward, or a not-yet-existing interval if walking backward.

Ground identity never comes from labels or mileage.  A fragment is identified
by its *birth*: the earliest season in which that physical ground exists and
its half-open interval on that season's axis.  Newly built ground is born on
the new axis and gets a fresh identity, so rebuilding the same mileage or
reusing the same label later can never claim an old ground's ancestry (no
back-dating).

Corrections are handled naturally: every walk takes an explicit ``pins`` map
``edge -> version_id``.  Re-walking the same span with a corrected book
re-derives the lineage from immutable evidence; the old walk is not edited.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from chainage_catalog import Interval, as_fraction, fraction_to_db
from handover_book import (
    HandoverRegistry,
    clip as _clip,
    subtract as _subtract,
    total_length,
)


class LineageError(ValueError):
    """Raised when a lineage walk is structurally impossible."""


@dataclass(frozen=True)
class GroundFragment:
    """One contiguous piece of ground on one season's axis."""

    season: str
    interval: Interval
    birth_season: str
    birth_interval: Interval

    def birth_key(self) -> tuple[str, str, str]:
        return (
            self.birth_season,
            fraction_to_db(self.birth_interval.start),
            fraction_to_db(self.birth_interval.end),
        )

    def to_dict(self) -> dict:
        return {
            "season": self.season,
            "start": fraction_to_db(self.interval.start),
            "end": fraction_to_db(self.interval.end),
            "birth_season": self.birth_season,
            "birth_start": fraction_to_db(self.birth_interval.start),
            "birth_end": fraction_to_db(self.birth_interval.end),
        }


@dataclass(frozen=True)
class TraceStep:
    """What happened while crossing one handover edge."""

    edge: tuple[str, str]
    handover_version: str
    carried: tuple[GroundFragment, ...]
    lost: tuple[GroundFragment, ...]
    lost_length: Fraction

    def to_dict(self) -> dict:
        return {
            "edge": list(self.edge),
            "handover_version": self.handover_version,
            "carried": [f.to_dict() for f in self.carried],
            "lost": [f.to_dict() for f in self.lost],
            "lost_length": fraction_to_db(self.lost_length),
        }


@dataclass(frozen=True)
class TraceResult:
    source_season: str
    source_interval: Interval
    target_season: str
    fragments: tuple[GroundFragment, ...]
    steps: tuple[TraceStep, ...]

    @property
    def surviving_length(self) -> Fraction:
        return total_length(f.interval for f in self.fragments)

    @property
    def source_length(self) -> Fraction:
        return self.source_interval.length

    @property
    def total_lost_length(self) -> Fraction:
        return sum((s.lost_length for s in self.steps), Fraction(0))

    def loss_by_step(self) -> list[tuple[tuple[str, str], str, Fraction]]:
        return [(s.edge, s.handover_version, s.lost_length) for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "source_season": self.source_season,
            "source_start": fraction_to_db(self.source_interval.start),
            "source_end": fraction_to_db(self.source_interval.end),
            "target_season": self.target_season,
            "fragments": [f.to_dict() for f in self.fragments],
            "steps": [s.to_dict() for s in self.steps],
            "surviving_length": fraction_to_db(self.surviving_length),
            "total_lost_length": fraction_to_db(self.total_lost_length),
        }


class LineageTracer:
    """Walks pinned handover chains over an immutable handover registry."""

    def __init__(self, registry: HandoverRegistry):
        self.registry = registry

    # -- forward walk -------------------------------------------------
    def trace_forward(
        self,
        source_season: str,
        interval,
        target_season: str,
        pins: Optional[dict] = None,
    ) -> TraceResult:
        interval = Interval(as_fraction(_start(interval)), as_fraction(_end(interval)))
        path = self.registry.edge_path(source_season, target_season)
        # Each live piece is (work interval on current axis, birth interval
        # on the fixed birth axis).  Inside one handover pair both intervals
        # move by the same slope-+1 map, so clipping the work interval tells
        # us exactly which birth sub-piece survives; the birth axis never moves.
        live: list[tuple[Interval, Interval]] = [(interval, interval)]
        steps: list[TraceStep] = []
        for edge in path:
            book = self.registry.resolve(edge, pins)
            carried: list[GroundFragment] = []
            lost: list[GroundFragment] = []
            for work, birth in live:
                frag_offset = birth.start - work.start
                # Only pairs that actually intersect this fragment are touched;
                # the book's sorted index makes this a binary-search window.
                for pair in book.pairs_overlapping_old(work):
                    piece = _clip(work, pair.old_interval)
                    if piece is None:
                        continue
                    new_iv = pair.map_forward_piece(piece)
                    birth_piece = Interval(
                        piece.start + frag_offset, piece.end + frag_offset
                    )
                    carried.append(
                        GroundFragment(
                            season=edge[1],
                            interval=new_iv,
                            birth_season=source_season,
                            birth_interval=birth_piece,
                        )
                    )
                # Retired = work parts not covered by any intersecting pair.
                covered = [
                    piece for pair in book.pairs_overlapping_old(work)
                    if (piece := _clip(work, pair.old_interval)) is not None
                ]
                for dead in _subtract(work, covered):
                    lost.append(
                        GroundFragment(
                            season=edge[0],
                            interval=dead,
                            birth_season=source_season,
                            birth_interval=Interval(
                                dead.start + frag_offset, dead.end + frag_offset
                            ),
                        )
                    )
            carried = _merge_fragments(carried)
            lost = _merge_fragments(lost)
            steps.append(
                TraceStep(
                    edge=edge,
                    handover_version=book.version_id,
                    carried=tuple(carried),
                    lost=tuple(lost),
                    lost_length=total_length(f.interval for f in lost),
                )
            )
            live = [(f.interval, f.birth_interval) for f in carried]
        return TraceResult(
            source_season=source_season,
            source_interval=interval,
            target_season=target_season,
            fragments=tuple(
                GroundFragment(
                    season=target_season,
                    interval=iv,
                    birth_season=source_season,
                    birth_interval=bv,
                )
                for iv, bv in live
            ),
            steps=tuple(steps),
        )

    # -- backward walk: where on the oldest axis did this ground come from
    def trace_back_origin(
        self,
        target_season: str,
        interval,
        pins: Optional[dict] = None,
        oldest_season: Optional[str] = None,
    ) -> tuple[list[GroundFragment], list[GroundFragment]]:
        """Return ``(inherited fragments, newly built fragments)``.

        Inherited fragments are traced back to their birth season; a fragment
        whose current axis portion is not paired at some edge is *born there*
        (newly built ground) and stops, so it can never be back-dated to an
        older season.  Each live piece carries its working interval on the
        current older axis, which becomes its birth interval only when the
        walk can go no further back.
        """
        interval = Interval(as_fraction(_start(interval)), as_fraction(_end(interval)))
        oldest = oldest_season or self.registry.seasons[0]
        # Live pieces: (season reached, working interval on that season's axis).
        live: list[tuple[str, Interval]] = [(target_season, interval)]
        born_here: list[GroundFragment] = []
        path = self.registry.edge_path(target_season, oldest)
        for edge in path:  # edge is (newer, older)
            book = self.registry.resolve((edge[1], edge[0]), pins)
            next_live: list[tuple[str, Interval]] = []
            for season, work in live:
                mapped, new_build = book.map_backward(work)
                for iv in mapped:
                    next_live.append((edge[1], iv))
                for iv in new_build:
                    # This ground first appears on the newer axis.
                    born_here.append(
                        GroundFragment(
                            season=edge[0],
                            interval=iv,
                            birth_season=edge[0],
                            birth_interval=iv,
                        )
                    )
            live = next_live
        # Anything still live reaches the oldest axis: it is born there.
        inherited = [
            GroundFragment(
                season=season,
                interval=iv,
                birth_season=season,
                birth_interval=iv,
            )
            for season, iv in live
        ]
        return _merge_fragments(inherited), _merge_fragments(born_here)

    # -- catalog-client entry point: full ancestry of an old span
    def trace_ground(
        self,
        source_season: str,
        interval,
        target_season: str,
        pins: Optional[dict] = None,
    ) -> TraceResult:
        """Forward trace with per-step loss accounting (the client query)."""
        return self.trace_forward(source_season, interval, target_season, pins)

    def origins_on_target(
        self,
        target_season: str,
        interval,
        pins: Optional[dict] = None,
    ) -> list[GroundFragment]:
        """Every piece of a target-axis interval with its resolved birth."""
        inherited, new_built = self.trace_back_origin(target_season, interval, pins)
        return merge_intervals_fragments(inherited + new_built)

    def birth_piece_at_target(
        self,
        birth_season: str,
        birth_piece: Interval,
        target_season: str,
        pins: Optional[dict] = None,
    ) -> Interval:
        """Affine-walk one birth-axis sub-piece forward to its target position.

        Raises if any part of the piece was retired before the target season;
        a surviving piece maps through exactly one pair per edge.
        """
        work = birth_piece
        for edge in self.registry.edge_path(birth_season, target_season):
            book = self.registry.resolve(edge, pins)
            mapped, leftover = book.map_forward(work)
            if leftover:
                raise LineageError(
                    f"birth piece {birth_piece} is retired at edge {edge}"
                )
            if len(mapped) != 1:
                raise LineageError(
                    f"birth piece {birth_piece} splits at edge {edge}; "
                    "callers must walk sub-pieces separately"
                )
            work = mapped[0]
        return work


def _start(interval) -> Fraction:
    return interval.start if isinstance(interval, Interval) else interval[0]


def _end(interval) -> Fraction:
    return interval.end if isinstance(interval, Interval) else interval[1]


def _merge_fragments(fragments: list[GroundFragment]) -> list[GroundFragment]:
    """Merge adjacent fragments that are pieces of one ground identity.

    Two fragments merge only when they share the *exact same* birth interval
    (same birth season, same half-open birth-axis span) and their target-axis
    intervals touch or overlap.  Fragments with different birth intervals are
    different pieces of ground and always stay apart, however close they sit
    on the target axis -- merging them would manufacture one identity from
    two.  Output keeps one fragment per distinct ground piece.
    """
    ordered = sorted(
        fragments,
        key=lambda f: (
            f.season, f.birth_season,
            f.birth_interval.start, f.birth_interval.end, f.interval.start,
        ),
    )
    out: list[GroundFragment] = []
    for frag in ordered:
        if out:
            prev = out[-1]
            same_identity = (
                prev.season == frag.season
                and prev.birth_season == frag.birth_season
                and prev.birth_interval == frag.birth_interval
                and frag.interval.start <= prev.interval.end
            )
            if same_identity:
                out[-1] = GroundFragment(
                    season=prev.season,
                    interval=Interval(
                        prev.interval.start, max(prev.interval.end, frag.interval.end)
                    ),
                    birth_season=prev.birth_season,
                    birth_interval=prev.birth_interval,
                )
                continue
        out.append(frag)
    return out


def merge_intervals_fragments(fragments: list[GroundFragment]) -> list[GroundFragment]:
    return _merge_fragments(fragments)
