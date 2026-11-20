"""Cross-season handover books: which physical ground survives a re-measure.

A park re-measures its trail every quarter.  Each measurement gets its own
physical axis (a :class:`~chainage_catalog.RouteCatalog` revision); a label
or a mileage value on a new axis is *not* evidence that the ground is the
same ground.  The only evidence of identity is a **handover book**: an
explicit, audited list of paired half-open intervals

    old axis [a, b)  ==  new axis [c, d)        (same ground)

with the invariants demanded by the problem statement:

* every pair has equal length and the same orientation (slope +1);
* the old sides are mutually non-overlapping and so are the new sides;
* parts of the old axis that no pair claims are **retired ground**;
* parts of the new axis that no pair claims are **newly built ground**.

A handover book is immutable once published.  Re-surveys make mistakes, so a
later *correction version* of the same edge may supersede an earlier one;
old versions are kept as evidence forever (vouchers pin the exact version
they were settled against), and every computation accepts an explicit pin
map so a signed batch can always be re-explained offline.

Ground identity across many seasons is the chain of pair mappings.  A piece
of ground is identified by where it was **born** -- the earliest season in
which some axis interval of it exists -- plus its half-open interval on that
birth axis.  Newly built ground gets a fresh identity: if a later season
rebuilds ground that happens to carry the same mileage or label, nothing
makes it the same physical ground, and it can never be back-dated.

Numbers stay :class:`fractions.Fraction` end to end; every boundary is exact.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from chainage_catalog import Interval, as_fraction, fraction_to_db, merge_intervals


class HandoverError(ValueError):
    """Base error for handover book construction and lookup."""


# ---------------------------------------------------------------------------
# Half-open interval geometry (all inputs are exact Fractions)
# ---------------------------------------------------------------------------


def clip(interval: Interval, window: Interval) -> Optional[Interval]:
    """Return ``interval ∩ window`` or ``None`` (half-open, touching is empty)."""
    lo = max(interval.start, window.start)
    hi = min(interval.end, window.end)
    if lo < hi:
        return Interval(lo, hi)
    return None


def subtract(interval: Interval, covered: Sequence[Interval]) -> list[Interval]:
    """Pieces of ``interval`` not covered by the (merged) ``covered`` union."""
    out: list[Interval] = []
    cursor = interval.start
    for win in merge_intervals(covered):
        if win.end <= cursor:
            continue
        if win.start >= interval.end:
            break
        if win.start > cursor:
            out.append(Interval(cursor, min(win.start, interval.end)))
        cursor = max(cursor, win.end)
        if cursor >= interval.end:
            break
    if cursor < interval.end:
        out.append(Interval(cursor, interval.end))
    return out


def total_length(intervals: Iterable[Interval]) -> Fraction:
    return sum((iv.length for iv in intervals), Fraction(0))


# ---------------------------------------------------------------------------
# Handover pairs and immutable book versions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandoverPair:
    """One equal-length, same-direction identity statement between two axes."""

    old_start: Fraction
    old_end: Fraction
    new_start: Fraction
    new_end: Fraction

    @property
    def old_interval(self) -> Interval:
        return Interval(self.old_start, self.old_end)

    @property
    def new_interval(self) -> Interval:
        return Interval(self.new_start, self.new_end)

    @property
    def shift(self) -> Fraction:
        """new coordinate = old coordinate + shift (same orientation)."""
        return self.new_start - self.old_start

    def map_forward_piece(self, piece: Interval) -> Interval:
        clipped = clip(piece, self.old_interval)
        if clipped is None:
            raise HandoverError("piece does not intersect this pair's old side")
        return Interval(clipped.start + self.shift, clipped.end + self.shift)

    def map_backward_piece(self, piece: Interval) -> Interval:
        clipped = clip(piece, self.new_interval)
        if clipped is None:
            raise HandoverError("piece does not intersect this pair's new side")
        return Interval(clipped.start - self.shift, clipped.end - self.shift)

    def to_dict(self) -> dict:
        return {
            "old_start": fraction_to_db(self.old_start),
            "old_end": fraction_to_db(self.old_end),
            "new_start": fraction_to_db(self.new_start),
            "new_end": fraction_to_db(self.new_end),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HandoverPair":
        return cls(
            old_start=as_fraction(data["old_start"]),
            old_end=as_fraction(data["old_end"]),
            new_start=as_fraction(data["new_start"]),
            new_end=as_fraction(data["new_end"]),
        )


@dataclass(frozen=True)
class HandoverVersion:
    """An immutable, fully validated handover book for one adjacent edge.

    ``supersedes`` names the previous version this book corrects (purely
    informational: versions never mutate, the registry just re-points
    ``current``).
    """

    version_id: str
    season_from: str
    season_to: str
    length_from: Fraction
    length_to: Fraction
    pairs: tuple[HandoverPair, ...]
    supersedes: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "length_from", as_fraction(self.length_from))
        object.__setattr__(self, "length_to", as_fraction(self.length_to))
        if self.length_from <= 0 or self.length_to <= 0:
            raise HandoverError("axis lengths must be positive")
        # An empty pair list is legal and meaningful: every old part is
        # retired and every new part is newly built ground.
        old_ivs, new_ivs = [], []
        for i, p in enumerate(self.pairs):
            if not p.old_start < p.old_end:
                raise HandoverError(f"pair {i} old side has no positive length")
            if not p.new_start < p.new_end:
                raise HandoverError(f"pair {i} new side has no positive length")
            if p.old_end - p.old_start != p.new_end - p.new_start:
                raise HandoverError(
                    f"pair {i}: old length {p.old_end - p.old_start} != "
                    f"new length {p.new_end - p.new_start}"
                )
            # Same direction is guaranteed by construction: both sides are
            # ascending half-open intervals and the affine map is slope +1.
            if not (Fraction(0) <= p.old_start and p.old_end <= self.length_from):
                raise HandoverError(f"pair {i} old side outside old axis [0, {self.length_from})")
            if not (Fraction(0) <= p.new_start and p.new_end <= self.length_to):
                raise HandoverError(f"pair {i} new side outside new axis [0, {self.length_to})")
            old_ivs.append(p.old_interval)
            new_ivs.append(p.new_interval)
        self._assert_disjoint(old_ivs, side="old")
        self._assert_disjoint(new_ivs, side="new")
        # Sorted copies are the lookup index; built once, reused forever.
        old_index = tuple(sorted(self.pairs, key=lambda p: (p.old_start, p.old_end)))
        new_index = tuple(sorted(self.pairs, key=lambda p: (p.new_start, p.new_end)))
        object.__setattr__(self, "_old_index", old_index)
        object.__setattr__(self, "_new_index", new_index)
        # Cached start-coordinate arrays turn lookup into binary search.
        object.__setattr__(
            self, "_old_starts", tuple(p.old_start for p in old_index)
        )
        object.__setattr__(
            self, "_new_starts", tuple(p.new_start for p in new_index)
        )

    @staticmethod
    def _assert_disjoint(intervals: list[Interval], side: str) -> None:
        ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.start < prev.end:  # touching (==) is legal for half-open tiles
                raise HandoverError(
                    f"handover {side} sides overlap: {prev} and {nxt}"
                )

    # -- axis bookkeeping --------------------------------------------
    def old_axis(self) -> Interval:
        return Interval(Fraction(0), self.length_from)

    def new_axis(self) -> Interval:
        return Interval(Fraction(0), self.length_to)

    def retired_parts(self) -> list[Interval]:
        """Old-axis ground no pair carries forward."""
        return subtract(self.old_axis(), [p.old_interval for p in self.pairs])

    def new_build_parts(self) -> list[Interval]:
        """New-axis ground no pair derives from the old axis."""
        return subtract(self.new_axis(), [p.new_interval for p in self.pairs])

    # -- indexed mappings --------------------------------------------
    def pairs_overlapping_old(self, interval: Interval) -> list[HandoverPair]:
        """Pairs whose old side intersects ``interval`` (binary-search narrowed).

        Disjoint sorted sides mean only a pair starting before ``interval.end``
        can intersect; the walk stops at the first pair starting at/after it.
        """
        hi = bisect.bisect_left(self._old_starts, interval.end)
        out = []
        for p in self._old_index[:hi]:
            if p.old_start < interval.end and interval.start < p.old_end:
                out.append(p)
        return out

    def pairs_overlapping_new(self, interval: Interval) -> list[HandoverPair]:
        """Pairs whose new side intersects ``interval``."""
        hi = bisect.bisect_left(self._new_starts, interval.end)
        out = []
        for p in self._new_index[:hi]:
            if p.new_start < interval.end and interval.start < p.new_end:
                out.append(p)
        return out

    def map_forward(self, interval: Interval) -> tuple[list[Interval], list[Interval]]:
        """Split an old-axis interval into (mapped new pieces, retired pieces)."""
        if not (Fraction(0) <= interval.start and interval.end <= self.length_from):
            raise HandoverError(
                f"interval {interval} outside old axis [0, {self.length_from})"
            )
        mapped: list[Interval] = []
        covered: list[Interval] = []
        # Only pairs starting before interval.end can intersect; binary search
        # narrows the scan to the local neighbourhood of the query.
        hi = bisect.bisect_left(self._old_starts, interval.end)
        for p in self._old_index[:hi]:
            clipped = clip(interval, p.old_interval)
            if clipped is not None:
                mapped.append(p.map_forward_piece(clipped))
                covered.append(clipped)
        retired = subtract(interval, covered)
        return merge_intervals(mapped), retired

    def map_backward(self, interval: Interval) -> tuple[list[Interval], list[Interval]]:
        """Split a new-axis interval into (mapped old pieces, new-build pieces)."""
        if not (Fraction(0) <= interval.start and interval.end <= self.length_to):
            raise HandoverError(
                f"interval {interval} outside new axis [0, {self.length_to})"
            )
        mapped: list[Interval] = []
        covered: list[Interval] = []
        hi = bisect.bisect_left(self._new_starts, interval.end)
        for p in self._new_index[:hi]:
            clipped = clip(interval, p.new_interval)
            if clipped is not None:
                mapped.append(p.map_backward_piece(clipped))
                covered.append(clipped)
        new_built = subtract(interval, covered)
        return merge_intervals(mapped), new_built

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "season_from": self.season_from,
            "season_to": self.season_to,
            "length_from": fraction_to_db(self.length_from),
            "length_to": fraction_to_db(self.length_to),
            "supersedes": self.supersedes,
            "pairs": [p.to_dict() for p in self.pairs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HandoverVersion":
        return cls(
            version_id=str(data["version_id"]),
            season_from=str(data["season_from"]),
            season_to=str(data["season_to"]),
            length_from=as_fraction(data["length_from"]),
            length_to=as_fraction(data["length_to"]),
            pairs=tuple(HandoverPair.from_dict(p) for p in data["pairs"]),
            supersedes=data.get("supersedes"),
        )


# ---------------------------------------------------------------------------
# Version registry: one ordered edge per adjacent season pair, corrections
# append versions and re-point current; nothing is ever deleted.
# ---------------------------------------------------------------------------


class HandoverRegistry:
    def __init__(self):
        self._versions: dict[str, HandoverVersion] = {}
        self._edges: dict[tuple[str, str], list[str]] = {}
        self._seasons: list[str] = []

    def add(self, version: HandoverVersion) -> HandoverVersion:
        if version.version_id in self._versions:
            raise HandoverError(f"handover version {version.version_id!r} already exists")
        edge = (version.season_from, version.season_to)
        prior = self._edges.get(edge, [])
        if prior:
            head = self._versions[prior[-1]]
            if (head.length_from != version.length_from
                    or head.length_to != version.length_to):
                raise HandoverError(
                    f"correction {version.version_id!r} changes axis lengths; "
                    "a correction re-pairs the same two axes, it does not move them"
                )
            if version.supersedes != head.version_id:
                raise HandoverError(
                    f"correction {version.version_id!r} must supersede "
                    f"{head.version_id!r}, got {version.supersedes!r}"
                )
        elif version.supersedes is not None:
            raise HandoverError(
                f"first version of edge {edge} cannot supersede {version.supersedes!r}"
            )
        self._register_season(version.season_from)
        self._register_season(version.season_to, after=version.season_from)
        self._versions[version.version_id] = version
        self._edges.setdefault(edge, []).append(version.version_id)
        return version

    def _register_season(self, season: str, after: Optional[str] = None) -> None:
        if season in self._seasons:
            if after is not None:
                if after not in self._seasons:
                    raise HandoverError(f"unknown season {after!r}")
                if self._seasons.index(season) != self._seasons.index(after) + 1:
                    raise HandoverError(
                        f"edge {after!r}->{season!r} breaks the measured season order "
                        f"{self._seasons}"
                    )
            return
        if after is None:
            self._seasons.append(season)
        else:
            if self._seasons[-1] != after:
                raise HandoverError(
                    f"new season {season!r} must follow {after!r} immediately; "
                    f"known order is {self._seasons}"
                )
            self._seasons.append(season)

    def get(self, version_id: str) -> HandoverVersion:
        try:
            return self._versions[version_id]
        except KeyError as exc:
            raise HandoverError(f"unknown handover version {version_id!r}") from exc

    def versions(self, season_from: str, season_to: str) -> list[HandoverVersion]:
        return [self._versions[v] for v in self._edges[(season_from, season_to)]]

    def current(self, season_from: str, season_to: str) -> HandoverVersion:
        try:
            return self._versions[self._edges[(season_from, season_to)][-1]]
        except KeyError as exc:
            raise HandoverError(
                f"no handover book registered for edge {season_from!r}->{season_to!r}"
            ) from exc

    def resolve(self, edge: tuple[str, str], pins: Optional[dict] = None) -> HandoverVersion:
        """Pick the pinned version for an edge, else the current one."""
        pins = pins or {}
        if edge in pins:
            return self.get(pins[edge])
        return self.current(*edge)

    @property
    def seasons(self) -> list[str]:
        return list(self._seasons)

    def edge_path(self, source: str, target: str) -> list[tuple[str, str]]:
        """Adjacent edges walked from ``source`` to ``target`` (either direction)."""
        if source not in self._seasons or target not in self._seasons:
            raise HandoverError(f"unknown season in path {source!r}->{target!r}")
        i, j = self._seasons.index(source), self._seasons.index(target)
        if i == j:
            return []
        step = 1 if j > i else -1
        return [
            (self._seasons[k], self._seasons[k + step])
            for k in range(i, j, step)
        ]

    # -- snapshots (offline evidence) --------------------------------
    def to_dict(self) -> dict:
        return {
            "seasons": list(self._seasons),
            "versions": [self._versions[v].to_dict()
                         for edge in self._edges.values() for v in edge],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HandoverRegistry":
        reg = cls()
        # Insertion order in the snapshot is the version chain order.
        for vdata in data["versions"]:
            reg.add(HandoverVersion.from_dict(vdata))
        return reg

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "HandoverRegistry":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
