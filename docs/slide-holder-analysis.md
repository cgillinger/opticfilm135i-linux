# The mounted-slide holder, as far as an empty holder allows

This documents what the mounted-slide holder does mechanically, established
2026-09-17 with an **empty** holder — no mounted slide was available. It
covers the load flow, holder identification, and how the vendor reaches the
four slide positions. It does **not** cover imaging (focus, sharpness,
colour, infrared, dust) or the real behaviour of a crop detector, because
those cannot be observed without a physical slide in the holder.

Two passes were made, mirroring `docs/holder-hardware-plan.md` part 2:

- **S2** — a read-only `of135i doctor` with the empty holder loaded through
  the driver, compared against a strip-holder load.
- **S1** — a vendor capture (QuickScan, empty holder, positions 1 and 4)
  with usbmon recording, decoded with `tools/compile_trace.py`.

## 1. The load flow is identical to the strip holder

The vendor's load sequence for the slide holder is byte-identical to the
strip holder's: the same motor batches (register `0x02` mode `0x18`/`0x1c`)
carrying the same FEEDL constants in `0x3d:0x3e:0x3f` (big-endian):

| move | FEEDL | note |
|---|---|---|
| OPEN | `0x1a22` = 6690 | the strip holder's cold-init constant |
| JOG | `0x0c12` = 3090 | the strip holder's jog constant |
| LOAD traverse | `0x11742` = 71490 | `FEEDL_CEILING`, the strip holder's traverse |

The driver's own `load` and `eject` were run on the real slide holder the
same day and both completed normally. So the slide holder loads, latches
and ejects exactly as the strip holder does — the load flow needs no
slide-specific work.

## 2. No holder identification we can read

`doctor` with the slide holder loaded differs from a strip-holder load in
exactly four registers — `0x2e`, `0x109`, `0x10a`, `0x120` — and every one
of them varies just as much *between strip-holder loads themselves*
(`0x2e`: 0x0b/0x07/0x07; `0x10a`: 0xaa/0xb6/0xd0; `0x120`: 0x14/0x0c/0x00).
They are motor/counter rests that depend on where the transport stopped,
not on the holder. The status word (`0xdc55`), `0x01` (`0x22`) and every
stable register are identical. The vendor read nothing holder-specific
either.

So the vendor's "automatically identifies film type" is **not a register
we can read**. If the scanner discriminates holders at all, it does so
mechanically/optically (the identifying tab holes) or inside firmware, in a
way no software query exposes — and the strip and panorama holders are
already known to share tab encoding, so any such signal is coarse at most.
This treats holder type as unknowable to software, which is what
`of135i/holder.py` already assumes.

## 3. There is no per-frame FEEDL grid — one sweep, cropped in software

The strip holder is positioned frame by frame: the vendor commands a
mode-`0x18` move to each frame's FEEDL on a constant 10752-step grid
(`docs/holder-geometry.md` section 3). **The slide holder is not.**

Across the whole capture there is not a single mode-`0x18` repositioning
move between or during the scan passes. Every pass — two previews and one
large scan — runs with FEEDL = 1 (scan in place). After the one LOAD
traverse (71490, the same transport reference as the strip holder), nothing
moves the transport to a "position 1" or "position 4". The large pass is a
single continuous sweep (36386 lines, ~565 MB) spanning the whole run, and
QuickScan finds the slide openings in that image **in software** — its
auto-multicrop. On the empty holder that multicrop *failed*, for the same
reason the whole approach depends on: with no slide there are no frame edges
to find.

This is the panorama mechanism (one long scan, software crop), now
confirmed for the slide holder. There is no slide pitch, no four-position
grid, nothing to reverse-engineer into a motor table.

## 4. Consequence for the driver

The four "positions" of the slide holder are software crops of one sweep,
not motor targets. A driver-side slide scan is therefore:

- **load** — already implemented and verified, identical to the strip holder;
- **one long transport sweep** — not four `POSITION` moves; the driver has
  no sweep-scan mode today, so this is the one piece of new transport work;
- **find the slides in the image** — a crop step in software.

The last step is not new reverse engineering: `of135i/aperture.py` and
`of135i/aperture_crop.py` already find aperture edges and crop for the strip
holder's overscan path, and can be adapted to the slide frame's geometry.

## 5. With a real slide (Test 84, 2026-09-18) — what changed

A mounted slide in holder position 1 was scanned with the strip holder's
frames 1–6 at 600 dpi (dual-light, IR), with no slide-specific code
(`docs/test-log.md`, Test 84). Findings:

- **Load, six POSITION moves and eject all ran normally** on the slide
  holder with existing code; calibration identical to the strip holder.
- **The slide's image is whole and unclipped** inside window 1: mount
  aperture 35.1 × 22.7 mm, i.e. the long side runs along the transport,
  the same orientation as a strip frame. The slide is IR-transparent, so
  the IR frame is a clean dust map.
- **The holder does have a grid** — the vendor simply chooses not to use
  it. Windows 2–6 caught the three empty openings as light straight
  through: openings 38.05 × 25.5 mm on a 62.6 mm pitch, starting at
  ≈ 23.5 / 86.1 / 148.7 / 211.4 mm (600-dual coordinate, n = 1 load),
  the last ending just inside the 71490 FEEDL ceiling. The vendor's
  257 mm sweep length (section 3) matches that span.

**Consequence for the driver.** Section 4's "one long sweep" is no longer
the only route: the four openings sit inside the FEEDL range the
POSITION mechanism is already verified on, so a driver slide scan can be
load + POSITION to the opening + the usual pass + PARK, per slide, reusing
everything the strip path has. The crop step should key on the IR frame
where available (the mount is opaque to IR, the film is not; visible-light
thresholds were fooled by dark image content).

**Still open:** focus and sharpness at the film plane (needs a
3600-class run), dust cleaning at a tuned resolution (`remove_dust` is
dpi-blind — Test 84), the crop step in code, positive-film rendering
(raw data healthy; rendering is the application's), and grid stability
across loads.
