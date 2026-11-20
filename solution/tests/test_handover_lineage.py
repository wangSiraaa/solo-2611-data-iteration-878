"""Unit tests for cross-season handover books and ground lineage.

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
from handover_book import (  # noqa: E402
    HandoverError,
    HandoverPair,
    HandoverRegistry,
    HandoverVersion,
    clip,
    subtract,
)
from ground_lineage import LineageError, LineageTracer  # noqa: E402


def pair(a, b, c, d):
    return HandoverPair(Fraction(a), Fraction(b), Fraction(c), Fraction(d))


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


class TestGeometry(unittest.TestCase):
    def test_clip_half_open_touching_is_empty(self):
        self.assertIsNone(
            clip(Interval(Fraction(5), Fraction(6)),
                 Interval(Fraction(6), Fraction(7)))
        )
        self.assertEqual(
            clip(Interval(Fraction(0), Fraction(10)),
                 Interval(Fraction(4), Fraction(7))),
            Interval(Fraction(4), Fraction(7)),
        )

    def test_subtract_keeps_measure(self):
        rest = subtract(
            Interval(Fraction(0), Fraction(10)),
            [Interval(Fraction(2), Fraction(5)), Interval(Fraction(7), Fraction(9))],
        )
        self.assertEqual(
            rest,
            [Interval(Fraction(0), Fraction(2)),
             Interval(Fraction(5), Fraction(7)),
             Interval(Fraction(9), Fraction(10))],
        )


# ---------------------------------------------------------------------------
# Handover book validation
# ---------------------------------------------------------------------------


class TestHandoverValidation(unittest.TestCase):
    def _book(self, pairs, **kw):
        return HandoverVersion(
            kw.get("vid", "H"), "A", "B",
            kw.get("lf", 10), kw.get("lt", 10), tuple(pairs),
            kw.get("sup"),
        )

    def test_unequal_length_rejected(self):
        with self.assertRaises(HandoverError):
            self._book([pair(0, 3, 0, 4)])

    def test_overlapping_old_sides_rejected(self):
        with self.assertRaises(HandoverError):
            self._book([pair(0, 4, 0, 4), pair(3, 6, 5, 8)])

    def test_touching_sides_are_legal(self):
        book = self._book([pair(0, 4, 2, 6), pair(4, 6, 0, 2)])
        self.assertEqual(book.retired_parts(),
                         [Interval(Fraction(6), Fraction(10))])

    def test_pair_outside_axis_rejected(self):
        with self.assertRaises(HandoverError):
            self._book([pair(8, 10, 0, 2)], lf=9)

    def test_empty_book_means_full_retire_and_full_new_build(self):
        book = self._book([], lf=5, lt=7)
        self.assertEqual(book.retired_parts(), [Interval(Fraction(0), Fraction(5))])
        self.assertEqual(book.new_build_parts(),
                         [Interval(Fraction(0), Fraction(7))])

    def test_retired_and_new_build_partition(self):
        book = self._book([pair(0, 3, 6, 9)], lf=8, lt=9)
        self.assertEqual(book.retired_parts(),
                         [Interval(Fraction(3), Fraction(8))])
        self.assertEqual(book.new_build_parts(),
                         [Interval(Fraction(0), Fraction(6))])


# ---------------------------------------------------------------------------
# Forward / backward mapping and exact rationals
# ---------------------------------------------------------------------------


class TestMapping(unittest.TestCase):
    def setUp(self):
        self.book = HandoverVersion("H", "A", "B", Fraction(8), Fraction(9), (
            pair(0, 3, 0, 3), pair(5, 8, 6, 9),
        ))

    def test_forward_split_maps_and_retires(self):
        mapped, retired = self.book.map_forward(Interval(Fraction(0), Fraction(8)))
        self.assertEqual(mapped,
                         [Interval(Fraction(0), Fraction(3)),
                          Interval(Fraction(6), Fraction(9))])
        self.assertEqual(retired, [Interval(Fraction(3), Fraction(5))])

    def test_backward_split_detects_new_build(self):
        mapped, new_build = self.book.map_backward(
            Interval(Fraction(0), Fraction(9))
        )
        self.assertEqual(mapped,
                         [Interval(Fraction(0), Fraction(3)),
                          Interval(Fraction(5), Fraction(8))])
        self.assertEqual(new_build, [Interval(Fraction(3), Fraction(6))])

    def test_fractional_boundaries_exact(self):
        book = HandoverVersion("Hf", "A", "B", Fraction(2), Fraction(2), (
            HandoverPair(Fraction(1, 2), Fraction(3, 2),
                         Fraction(1, 2), Fraction(3, 2)),
        ))
        mapped, retired = book.map_forward(Interval(Fraction(0), Fraction(2)))
        self.assertEqual(mapped, [Interval(Fraction(1, 2), Fraction(3, 2))])
        self.assertEqual(retired,
                         [Interval(Fraction(0), Fraction(1, 2)),
                          Interval(Fraction(3, 2), Fraction(2))])


# ---------------------------------------------------------------------------
# Registry: corrections and season ordering
# ---------------------------------------------------------------------------


class TestRegistry(unittest.TestCase):
    def test_correction_chain_and_current(self):
        reg = HandoverRegistry()
        v1 = HandoverVersion("v1", "A", "B", 10, 10, (pair(0, 4, 0, 4),))
        reg.add(v1)
        v2 = HandoverVersion("v2", "A", "B", 10, 10,
                             (pair(0, 5, 0, 5),), supersedes="v1")
        reg.add(v2)
        self.assertEqual(reg.current("A", "B").version_id, "v2")
        self.assertEqual([v.version_id for v in reg.versions("A", "B")],
                         ["v1", "v2"])
        # Old version stays retrievable forever (evidence).
        self.assertEqual(reg.get("v1").pairs[0].old_end, Fraction(4))

    def test_correction_cannot_change_axis_length(self):
        reg = HandoverRegistry()
        reg.add(HandoverVersion("v1", "A", "B", 10, 10, (pair(0, 4, 0, 4),)))
        with self.assertRaises(HandoverError):
            reg.add(HandoverVersion("v2", "A", "B", 10, 11,
                                    (pair(0, 4, 0, 4),), supersedes="v1"))

    def test_correction_must_name_its_predecessor(self):
        reg = HandoverRegistry()
        reg.add(HandoverVersion("v1", "A", "B", 10, 10, (pair(0, 4, 0, 4),)))
        with self.assertRaises(HandoverError):
            reg.add(HandoverVersion("v2", "A", "B", 10, 10,
                                    (pair(0, 4, 0, 4),)))

    def test_edge_path_both_directions(self):
        reg = HandoverRegistry()
        reg.add(HandoverVersion("h1", "Q1", "Q2", 5, 5, (pair(0, 2, 0, 2),)))
        reg.add(HandoverVersion("h2", "Q2", "Q3", 5, 5, (pair(0, 2, 0, 2),)))
        self.assertEqual(reg.edge_path("Q1", "Q3"), [("Q1", "Q2"), ("Q2", "Q3")])
        self.assertEqual(reg.edge_path("Q3", "Q1"), [("Q3", "Q2"), ("Q2", "Q1")])

    def test_json_roundtrip(self):
        reg = HandoverRegistry()
        reg.add(HandoverVersion("v1", "A", "B", 10, 10, (pair(0, 4, 0, 4),)))
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "h.json")
            reg.save_json(path)
            back = HandoverRegistry.load_json(path)
        self.assertEqual(back.current("A", "B").pairs, reg.current("A", "B").pairs)


# ---------------------------------------------------------------------------
# Multi-season lineage, per-step loss, no back-dating
# ---------------------------------------------------------------------------


def _three_season_registry():
    reg = HandoverRegistry()
    # Q1 -> Q2
    reg.add(HandoverVersion("h12", "Q1", "Q2", 12, 12, (
        pair(0, 4, 0, 4), pair(6, 10, 8, 12),
    )))
    # Q2 -> Q3 (Q3 length 13)
    reg.add(HandoverVersion("h23", "Q2", "Q3", 12, 13, (
        pair(0, 5, 0, 5), pair(8, 11, 5, 8),
    )))
    return reg


class TestLineage(unittest.TestCase):
    def setUp(self):
        self.reg = _three_season_registry()
        self.tracer = LineageTracer(self.reg)
        self.pins = {("Q1", "Q2"): "h12", ("Q2", "Q3"): "h23"}

    def test_forward_fragments_and_birth_identity(self):
        tr = self.tracer.trace_forward(
            "Q1", Interval(Fraction(0), Fraction(12)), "Q3", self.pins
        )
        targets = sorted((f.interval for f in tr.fragments),
                         key=lambda iv: (iv.start, iv.end))
        self.assertEqual(targets,
                         [Interval(Fraction(0), Fraction(4)),
                          Interval(Fraction(5), Fraction(8))])
        # Birth intervals are exact sub-pieces, not the whole source span.
        births = sorted((f.birth_interval for f in tr.fragments),
                        key=lambda iv: (iv.start, iv.end))
        self.assertEqual(births,
                         [Interval(Fraction(0), Fraction(4)),
                          Interval(Fraction(6), Fraction(9))])

    def test_loss_accounted_per_step(self):
        tr = self.tracer.trace_forward(
            "Q1", Interval(Fraction(0), Fraction(12)), "Q3", self.pins
        )
        losses = {edge: length for edge, _vid, length in tr.loss_by_step()}
        self.assertEqual(losses[("Q1", "Q2")], Fraction(4))
        self.assertEqual(losses[("Q2", "Q3")], Fraction(1))
        self.assertEqual(tr.surviving_length + tr.total_lost_length,
                         Fraction(12))

    def test_new_build_is_not_back_dated(self):
        inherited, new_built = self.tracer.trace_back_origin(
            "Q3", Interval(Fraction(9), Fraction(13)), pins=self.pins
        )
        self.assertEqual(inherited, [])
        self.assertEqual([f.birth_interval for f in new_built],
                         [Interval(Fraction(9), Fraction(13))])
        self.assertTrue(all(f.birth_season == "Q3" for f in new_built))

    def test_reused_mileage_gets_fresh_identity(self):
        # Ground at Q3 [11,12) under these books is new build; even if a later
        # season reuses the number, its ancestry never reaches Q1.
        inherited, _ = self.tracer.trace_back_origin(
            "Q3", Interval(Fraction(11), Fraction(12)), pins=self.pins
        )
        self.assertEqual(inherited, [])

    def test_birth_piece_walk_forward(self):
        target = self.tracer.birth_piece_at_target(
            "Q1", Interval(Fraction(6), Fraction(9)), "Q3", self.pins
        )
        self.assertEqual(target, Interval(Fraction(5), Fraction(8)))

    def test_retired_birth_piece_raises(self):
        with self.assertRaises(LineageError):
            self.tracer.birth_piece_at_target(
                "Q1", Interval(Fraction(4), Fraction(6)), "Q3", self.pins
            )

    def test_correction_changes_lineage_under_new_pins(self):
        self.reg.add(HandoverVersion("h12v2", "Q1", "Q2", 12, 12, (
            pair(0, 4, 0, 4), pair(4, 6, 4, 6), pair(6, 10, 6, 10),
        ), supersedes="h12"))
        pins = {("Q1", "Q2"): "h12v2", ("Q2", "Q3"): "h23"}
        tr = self.tracer.trace_forward(
            "Q1", Interval(Fraction(0), Fraction(12)), "Q3", pins
        )
        # Old [4,6) now survives to Q2 [4,6), but still retires at Q2->Q3.
        first_loss = tr.loss_by_step()[0][2]
        self.assertEqual(first_loss, Fraction(2))


if __name__ == "__main__":
    unittest.main()
