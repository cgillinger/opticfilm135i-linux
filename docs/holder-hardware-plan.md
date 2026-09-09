# Hardware plan: the six-frame strip holder, and the slide holder

Written 2026-09-09, offline. Nothing here has been run. It is the
minimum set of hardware runs that turns the offline analysis in
`docs/holder-geometry.md` into verified support for frames 1-6, plus the
smallest safe first step for the mounted-slide holder.

Standing rules for every run below, from `docs/hardware-safety.md` and
the project's own history:

- **Listen.** Any scraping or grinding: cut power immediately. Report
  the sound for every motor step, normal or not.
- **Never a blind motor command from an undefined mechanical state.**
  Every run starts from a power cycle and a completed `of135i load`.
- **Interactive steps run in a real terminal window.** The load flow
  prompts and `hwblock` waits on Enter; neither works through a tool
  without a tty.
- **Exit is defined.** After a completed pass: `of135i eject` from
  post-PARK. After an aborted one: power cycle, then `of135i load
  --double-jog`, then eject.
- **Verify ownership first.** `lsusb | grep 07b3` and
  `.venv/bin/python -m of135i status` (healthy idle: reg 0x01 = 0x22).
  The Windows VM must be off or disconnected.

`<analysis>` below is the private capture and analysis directory (scans
of real film are personal and never go in git); `<review-images>` is the
folder the operator's own eye checks are collected in.

---

## Part 1 — the strip holder

### N1. Empty holder, frames 1-6 at 600 dpi (one load)

**What it proves.** Where the transport actually stops relative to each
of the six apertures. Six signed numbers, one per frame: the distance
between the aperture's measured centre and the scan window's centre.

**Why it is needed.** It is the only measurement that separates the
transport (A) from the holder (B) with film (C) removed from the
question entirely. It settles the open pitch decision -- 10752 against
the driver's 10760 -- because a wrong pitch appears as a registration
error growing linearly along the strip, 0.028 mm per frame, 0.14 mm by
frame 6. And it produces the margin figures the definition of done is
written against.

**What it builds on.** Load verified 7/7 from power-on (Tests 17-23).
Frames 1-4 positioned and scanned correctly, ten repeats, geometry
±4 lines (Tests 17-24, 28). The 600 dpi dual profile hardware-verified
in the driver, and multi-frame batch hardware-verified at 3600 dpi.
Note honestly that **600 dpi in a multi-frame batch is a combination
that has not itself been run**; if it misbehaves, fall back to one
frame per invocation from a single load. Six apertures measured offline from the vendor's own
whole-holder pass, where positions 5 and 6 were empty
(`docs/holder-geometry.md`). Frames 5 and 6 driven by the vendor on this
unit, FEEDL 49796 and 60174, `segments/05-batch-komplett.pcap`.

**Why it is safe.** Frame 6's target (60546) is 10944 steps -- 38.6 mm
-- short of the load traverse (71490) that this transport performs on
every single load, so it is inside routine travel, not past it. The
vendor has driven to frames 5 and 6 on this unit already. Frame numbers
outside 1-6 are refused before any write, and a computed target above
71490 is refused independently. 600 dpi keeps each frame to about 5 MB
and each pass to a few seconds.

**Steps** (real terminal, from the repo root):

    # power cycle, magazine loose in the well, holder EMPTY -- no film
    .venv/bin/python -m of135i status            # expect 0x01 = 0x22
    .venv/bin/python -m of135i load              # prompts; expect f455/dc55
    .venv/bin/python -m of135i scan --frames 1-6 --dpi 600 \
        -o <analysis>/holder-<date>/empty-a.pnm
    .venv/bin/python -m of135i eject

Then, offline:

    for f in 1 2 3 4 5 6; do
      .venv/bin/python tools/holder_geometry.py frame \
        <analysis>/holder-<date>/empty-a-f$f.pnm \
        --dpi 600 --json .../empty-a-f$f.json \
        --control <review-images>/holder-a-f$f.png
    done

**Expected.** POSITION completes on class F for each frame, inside its
budget (4.8 s at frame 1, 28 s at frame 4, 35.8 s at frame 5, 43.5 s at
frame 6 -- the budget scales with the absolute target, so it is
generous; the vendor's own frame-4 move took 8.1 s). PARK completes on
the usual class after each frame; its two waits now scale with the same
factor, so frame 6 gets 23 s / 45 s rather than 15 s / 30 s. Every scan
delivers 878 lines of 876 px. Every frame's report says
`aperture_found: true` with both margins positive.

**Stop conditions.** Any scraping sound. A POSITION or PARK timeout (the
driver fails closed by itself: session FAILED, nothing further written,
power cycle required). `aperture_found: false` on any frame -- stop
before scanning further frames, because it means the window is not where
the analysis predicts.

**On deviation.** Power cycle. Then `of135i load --double-jog`, then
`of135i eject`. Fallback if the magazine stays latched: `load
--release`, power cycle, `load`, `eject`. Send me the JSON reports and
the control images; do not repeat the run before we have read them.

### N2. Repeatability: N1 again, twice more, over separate loads

**What it proves.** How much of the registration error is the transport
re-finding its reference, rather than the model. Frames 1 and 6 matter
most: they span the whole travel.

**Why it is needed.** The margin in the definition of done has to be
larger than this variation, or "it fits" is luck. Three loads is enough
if they agree; more only if they do not.

**What it builds on.** N1. The ±4-line (0.028 mm) figure already
observed across ten repeats at 3600 dpi gives the expected order.

**Steps.** N1 verbatim, twice more, each from its own power cycle and
its own load, with the holder taken out and put back between them.
Output prefixes `empty-b`, `empty-c`.

**Expected.** Per-frame centre offsets agreeing across the three loads
to within a few hundredths of a millimetre.

**Stop conditions and deviation handling.** As N1.

### N3. Six-frame colour negative, frames 1-6 at 3600 dpi

**What it proves.** The real thing: the right image in each of the six
positions, whole frame, no crossbar in the picture, no neighbour
bleeding in, normal sharpness and colour, delivery and park normal.

**Why it is needed.** It is the acceptance test for the feature. N1 and
N2 prove the geometry; this proves the product.

**What it builds on.** N1/N2 having passed. Plain 3600 dpi is the
tightest window of all: 0.08 mm per side against the mean aperture, and
slightly shorter than aperture 1 outright. It has no margin to spend on
a positioning error, which is exactly why the empty-holder run comes
first and why the pitch decision is taken before this run.

**Steps.** Power cycle, load with the six-frame colour strip, then

    .venv/bin/python -m of135i scan --frames 1-6 --dpi 3600 \
        --positive -o <analysis>/holder-<date>/colour.tiff
    .venv/bin/python -m of135i eject

**Expected.** Six images, each the right frame of the strip. Positives
to the review folder for your eye check -- with a caption per image
saying which frame it is and what to look at, so they can actually be
judged.

**Stop conditions.** As N1, plus: a crossbar visible inside the image
area on any frame. Stop and report rather than scanning the rest.

### N4. Infrared on frames 5 and 6 only

**What it proves.** That the new positions do not disturb the already
verified infrared path. Nothing more -- this is not a re-verification of
infrared.

**Steps.** Same strip, `--frames 5-6 --dpi 3600 --ir`. Two frames, not
six.

### N5. Six-frame black-and-white negative, frames 1-6

**What it proves.** The same six positions with a *different physical
strip*. If both strips land correctly, the model belongs to the holder
and the transport, not to one piece of film.

**Important.** Traditional silver black-and-white film is opaque to
infrared. This run is a geometry and framing control only; it is not an
infrared or dust-removal reference and must not be reported as one.

**Steps.** As N3, without `--ir`.

### N6 (optional). The four-frame strip in two holder positions

**What it proves.** The same negative scanned at two different holder
positions -- left-aligned as frames 1-4, right-aligned as frames 3-6.

**Verdict: not needed.** With two full-length strips and the empty-holder
measurement, this adds a third route to a conclusion already reached
twice. Run it only if the holder grips the short strip completely
normally in the right-aligned position. No tape, no spacer, no
improvised fixing -- if it does not sit properly, skip it.

---

## Part 2 — the mounted-slide holder

Nothing is known about this holder: no capture, no FEEDL, no pitch, no
aperture geometry, no load behaviour. The strip holder's numbers must not
be assumed to carry over, and until something is captured **the driver
must not be pointed at it at all**. The order below is therefore strict.

### S1. Vendor capture with the empty slide holder (no driver involvement)

**What it proves.** Everything we currently lack: whether the vendor
software accepts the holder empty, whether any register reads
differently with it inserted (holder identification), the load flow, the
four positions' FEEDL, the pitch, the scan geometry, and park/eject.

**Why it is first.** This is the project's own rule, learned on the load
bug: when a mechanism is unexplained, record a fresh vendor capture
before writing anything. It is also the only way to get frame positions
for this holder without guessing a motor target -- which the safety
principle forbids outright.

**Steps.** In the Windows VM, with usbmon recording (`capture.sh`): open
QuickScan with the empty slide holder inserted, let it identify and
preview, scan positions 1 and 4, then eject. Kill `OpticFilm.exe`
afterwards -- it does not release the device when its window closes --
and disconnect the scanner from the VM.

**Expected.** A pcap from which `tools/compile_trace.py` and the FEEDL
extraction used in `docs/holder-geometry.md` section 3 give the slide
holder's own grid.

**Safety.** The vendor software drives; we only record. This is the
lowest-risk way to touch an unknown holder.

### S2. Read-only, with the slide holder inserted

**What it proves.** Whether any register we can read differs between the
two holders -- the software half of holder identification.

**Steps.** With the holder inserted and the scanner idle:
`.venv/bin/python -m of135i doctor`. Read-only session, no motor
command. Compare with the same output taken with the strip holder in.

### S3. Empty slide holder through the driver — gated on S1

**Only after S1 has produced the slide holder's own FEEDL grid**, and
only with the geometry entered in `of135i/holder.py` as a measured
profile rather than an assumed one: load, positions 1-4, geometry
measurement with `holder_geometry.py`, park, eject. Same structure as
N1.

**What this can establish:** holder identification (if S1/S2 found a
signal), load and transport, frame count, pitch, the four aperture
positions, scan geometry, repeatability, park and eject. That is enough
to call the holder's *mechanics* hardware-verified.

**What it cannot establish, and must be documented as unverified:**
focus at the film plane inside a mount, actual sharpness, positive-film
colour and tonal rendering, infrared on a real slide, dust removal, and
any effect of different mount thicknesses. Those need a physical slide,
and then only a small focused imaging test -- not new reverse
engineering.

---

## Order, and what to do first

    N1  →  N2  →  N3  →  N4  →  N5  →  (N6 optional)
    S1  →  S2  →  S3

N1 is the one that decides things. Everything after it is confirmation,
and the pitch decision is taken between N1 and N3, on the numbers N1
produces. S1 is independent of all of it and can be done whenever the VM
is convenient.
