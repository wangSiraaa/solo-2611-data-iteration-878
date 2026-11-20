# Route chainage catalog + inspection coverage facts

Python 3.11 / SQLite. Synthetic data only — no real railway standard is
involved. The route is a physical axis `[0, L)` tiled by positive-length
half-open segments; labels within a segment are `label = physical -
physical_start + label_start` (slope fixed at 1). Labels are **not** a
global coordinate: they may repeat and may have gaps.

## Why labels cannot be a continuous coordinate

The materials' r1 route:

| section | physical `[start, end)` | label `[start, end)` |
|---------|-------------------------|----------------------|
| P1      | `[0, 400)`              | `[0, 400)`           |
| P2      | `[400, 700)`            | `[300, 600)`         |
| P3      | `[700, 1000)`           | `[650, 950)`         |

* label `350` has **two** physical candidates: P1@350 and P2@450. Treating
  `350` as one coordinate would silently swap the inspected object.
* label `620` is a **gap** (P2 ends at 600, P3 starts at 650): unresolved.
* `620` resolves only after the r2 base correction (P2 label base 300 → 330
  widens P2's labels to `[330, 630)`).

The point reverse lookup therefore returns **all** candidates and never
picks a nearest value:

| candidates | status       |
|------------|--------------|
| 0          | `unresolved` |
| 1          | `resolved`   |
| >1         | `ambiguous`  |

A caller may pass an explicit `section_id` to disambiguate; an unknown
section is reported `unresolved`, never guessed.

## Half-open semantics (the subtle part)

Point membership and interval endpoints are treated differently, on purpose:

* A physical **point** exactly on a segment's right end does **not** belong
  to that segment (`start <= p < end`).
* An interval's **upper bound** may equal a segment's exclusive end label.
  Converting a span never re-tests point membership for its upper bound, so
  a legal end-of-section span is accepted. The resulting physical interval
  is still half-open, which correctly excludes a facility sitting exactly
  at the boundary.

The `merge_intervals` union merges overlapping **and touching** intervals,
so `sum(length)` is the true covered measure — repeated or overlapping
spans are never double counted.

## Facilities

Each facility has a stable `asset_id` and a fixed **physical** position that
does not move when a label base changes. A facility counts as covered only
when its physical point lies in the merged physical coverage
(`start <= p < end`). Facility hits are **never** derived from a label
point lookup, so an ambiguous or unresolved label can never manufacture a
hit.

## Independent catalog vs. inspection facts

`chainage_catalog.py` knows nothing about jobs or reports: it can resolve
points and convert intervals on its own. `inspection_library.py` depends on
it, not the reverse. Storage is two separate schemas:

* **catalog**: `route_revision`, `route_section` (exact rationals stored as
  `n/d` text plus a sortable key for indexed range prefiltering).
* **inspection facts**: `asset`, `report`, `report_span`,
  `report_interval`, `report_facility`, `applied_op`.

Reports record the original label request, the selected section, the actual
physical coverage, the covered facility identities, and the catalog
revision. `report_id` is content-addressed: identical content is idempotent,
differing content under the same id is rejected.

## Revision behaviour

`revise_chainage` changes **only** the named section's `label_start`
(honouring the material's explicit base correction). Physical extents,
section order, and every other section are copied verbatim — this is not a
route editor, and no other edits are inferred.

After `r2` (P2 base 300 → 330), the *same* request `P2 [350,390)`:

* r1: physical `[450, 490)` → covers **B**@470 (not C@440).
* r2: physical `[420, 460)` → covers **C**@440 (not B@470).

The facility identities and physical positions are unchanged; only the
label→physical mapping moved. Old report R1 keeps explaining the original
labels and positions (`stale=true` once r2 is current).

## Running

```bash
python3 solution/run_demo.py                    # in-memory
python3 solution/run_demo.py --db route.db      # durable (resumable)
python3 -m unittest discover -s solution/tests -v
python3 solution/bench_scale.py                 # 20 sections / 500 assets / 200 jobs
```

`run_demo.py` reads `materials/730` operation by operation, applies the P2
base correction, and prints the real facility change (B in r1 → C in r2).
A file-backed DB records an op-log so re-running resumes (mutating ops are
skipped, read-only ops still render).

## Scale / cost (20 sections, 500 facilities, 200 jobs)

Measured by `bench_scale.py`:

* **Point reverse lookup** — per-section scan, ~39 µs/query at 20 sections
  (~11 % ambiguous, ~88 % unresolvable in the stress route). A per-revision
  label-range index would make this `O(log n + k)` for the k duplicate
  candidates. Duplicate labels mean the result is a *set*, so no index can
  legitimately collapse it to one value.
* **Batch conversion** — ~0.18 ms/job: convert spans `O(#spans)`, merge
  `O(spans log spans)`, facility scan `O(#assets)`. 500 assets × 200 jobs =
  100k point checks; a physical-position index would reduce the facility
  side to an interval query per job.
* **Revision invalidation** — building the new revision is ~0.05 ms (copy
  the section rows). Marking old reports stale is metadata-only. The real
  work is re-resolving jobs that must be republished, `O(jobs × (spans +
  assets))`; in the stress route moving one section's base pushed 14/200
  spans outside the moved section's label range, and those jobs are
  correctly rejected rather than remapped.

## Explicit non-goals

No path optimisation, no measurement adjustment/least-squares, no inference
of missing positions. Nothing beyond the one explicit base correction is
changed.

---

# Cross-season handover, ground lineage and maintenance settlement

The catalog above resolves labels inside *one* measurement. Every quarter the
trail is re-measured onto a new axis, so three more modules answer the
cross-season questions: what ground survived, what it can still be paid for,
and how a later correction re-opens an already-signed batch.

```
chainage_catalog.py      one-season axes, label<->physical, exact Fractions
        |
handover_book.py         immutable, versioned old/new half-open interval pairs
        |
ground_lineage.py        walks the pinned chain; birth identity + per-step loss
        |
maintenance_ledger.py    contract clipping, once-only payment, vouchers,
                         signed batches, replay, correction advice
```

## Handover books (`handover_book.py`)

A book for one adjacent season edge is a list of pairs
`old [a,b) == new [c,d)` of the same ground. Validation enforces the problem
rules: equal length, same orientation (slope +1), mutually non-overlapping old
sides and new sides, everything inside the two axes. An **empty pair list is
legal**: the whole old axis is retired and the whole new axis newly built.

* old-axis parts no pair claims = **retired ground** (`retired_parts`);
* new-axis parts no pair claims = **newly built ground** (`new_build_parts`).

Books are immutable. A re-survey mistake is fixed by appending a **correction
version** that re-pairs the *same two axes* and names its predecessor via
`supersedes`; the registry re-points `current()` but every old version stays
readable and every voucher pins the exact version it used. A correction may
not change axis lengths or skip the predecessor chain.

Sorted pair-side indices plus a cached start array make `map_forward` /
`map_backward` a binary-search window: cost is `O(log p + k)` for the `k`
pairs actually intersecting the query, not a full-axis scan.

## Ground identity and lineage (`ground_lineage.py`)

Labels and mileage are never evidence of identity — only the handover chain
is. A fragment's identity is its **birth**: the earliest season whose axis
contains it plus its half-open interval there. The forward walk carries both
the current-axis working interval and the birth-axis interval, clipping both
through the same slope-+1 pair at every edge, so:

* `trace_forward(span, target)` returns every surviving target fragment and a
  `TraceStep` per edge recording exactly which pieces were carried and the
  length lost there (`loss_by_step`);
* `trace_back_origin` separates inherited ground from ground **born on the
  target axis**. New build is never back-dated: rebuilding the same mileage or
  reusing the same label later gets a fresh identity;
* `birth_piece_at_target` affine-walks one birth sub-piece forward and raises
  if it was retired before the target season.

Corrections are handled by the `pins` map (`edge -> version_id`). Re-walking
the same span under corrected books re-derives everything from immutable
evidence; the previous walk is never edited.

## Maintenance settlement (`maintenance_ledger.py`)

A `Contract` is non-overlapping priced half-open zones on one target season.
Settlement of a report:

1. map its intervals onto the target axis under the pinned books (a
   same-season report still runs the backward ancestry walk, so native new
   build is excluded);
2. clip to the priced contract zones;
3. keep only **inherited** ground — new build is not paid even when covered;
4. deduct ground already paid, keyed by birth identity. Within one batch the
   reports are allocated in the **explicit order given**, so the first report
   owns every contested overlap.

Each `VoucherLine` records the target ground paid, its birth ancestry, the
report, the zone/price and the exact handover versions. Money is exact
(`Fraction` length × price). A `SettlementBatch` is a frozen, JSON-serialisable
artifact; `replay_batch` re-runs the same reports, pins and prior ledger and
asserts the lines and total reproduce byte-for-byte, so a signed batch is
explainable offline.

### Corrections do not touch old vouchers

`advise_correction` re-simulates a signed batch under corrected pins and
compares voucher by voucher **on target-axis ground** (a fragment present
before and after is the same maintenance object even though its resolved
birth identity changed — that change is the erratum's content). It emits:

* `credit` (应补) and `debit` (应减) per voucher;
* `blocked` when a simulated extra credit's birth ground is already held by
  another confirmed voucher — the same physical ground is never paid twice,
  even through an erratum. Blocked amounts never enter net cash.

The original batch and vouchers remain unchanged until a human confirms the
advice.

## Running the new layer

```bash
python3 solution/run_cross_season.py            # worked example + 3-season demo
python3 solution/run_cross_season.py --out DIR  # also write JSON evidence
python3 solution/bench_cross_season.py          # 6 seasons / 300 reports stress
python3 -m unittest discover -s solution/tests  # all suites (92 tests)
```

### The worked example, reproduced exactly

Old report `[0,8)`, handover `old[0,3)->new[0,3)` and
`old[5,8)->new[6,9)`, a new middle section that merely reuses an old label,
contract new `[0,8)` at unit price 2:

| scenario | payable length | amount |
|---|---|---|
| no prior payments | 5 | **10** |
| ground `[6,7)` already paid | 4 | **8** |
| corrected book, second pair -> `new[5,8)`, empty ledger | 6 | **12** |

The new middle `[3,6)` is new-build ground: the demo's ancestry query reports
its birth season as the new season, so it contributes nothing even though it
carries an old label.

### Three-season demo coverage

`run_cross_season.py` Part B walks Q1→Q2→Q3 and deliberately covers:
half-open and rational zone boundaries; a repeated label (label 2 on two
sections in one season, disambiguated by the bound section, and label 5 reused
on brand-new Q3 ground); per-step loss over chained edges; same-batch order
allocation with the overlap paid once; native new-build excluded; signed-batch
replay from JSON; a correction producing per-voucher debit/credit; and a
correction whose extra credit is **blocked** by an external confirmed voucher.

