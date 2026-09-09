# Holder geometry: the six apertures, measured

Status: offline analysis, 2026-09-09. Everything below is measured from
captures already on disk; nothing in it required the scanner. The parts
that still need hardware are marked as such, and the hardware plan is in
section 8.

The driver has always positioned to frames 1-4 with a linear model,
`FEEDL = 6743 + (n-1) x 10760`. The strip holder actually takes six
frames. This document establishes, from evidence, where those six
positions are, how far apart they really are, and what the remaining
uncertainty is.

## 1. Why the holder can be measured without loading one

The vendor's per-resolution reference captures
(`captures/20260902-vendor-*dpi.pcap`) are not single-frame scans. As
recorded in protocol-notes.md, QuickScan calibrates and then runs the
scan pass straight from the loaded position **over the whole strip** --
about 254 mm of travel, the full length of the magazine. The image data
in those captures therefore contains the holder itself, end to end: the
opaque plastic crossbars, the open apertures, and the leading tab.

The strip loaded for those captures held four frames, so apertures 5 and
6 were **empty**. That is not a defect of the capture. It is the
empty-holder measurement, already made: in positions 5 and 6 nothing but
the holder was in the light path.

Measurement is done on the infrared pass (the even lines of a dual-light
scan). Colour film base is near-transparent to infrared, so the only
thing that modulates the signal along the strip is the holder's own
plastic. The along-strip profile is a square wave whose edges are the
physical aperture edges.

Tool: `tools/holder_geometry.py`. It reduces a scan to that profile,
places each edge by linear interpolation of the half-level crossing (so
an edge is located to a fraction of a line, not to the nearest line),
and reports apertures, crossbars, pitch and residuals as JSON. It can
also draw a control image with the measured edges marked, for when the
numbers alone are not convincing.

    .venv/bin/python tools/holder_geometry.py profile \
        <analysis>/dpi-data/600-profile.npy --dpi 600 --dual \
        --sub-mode strip --control holder.png

The tool's single-frame mode was checked against a known answer: a
882-line window was cut out of that same sweep around aperture 5 and
deliberately displaced by +10 lines. The tool read the displacement back
as -9.42 lines (the aperture sits 10 lines low in the window) and the
aperture length as 35.808 mm against the sweep's own 35.799 mm. So it
resolves a displacement to about 0.6 lines at 600 dpi, 0.025 mm --
comfortably finer than anything that has to be decided below.

## 2. The six apertures (measured)

600 dpi, infrared pass, central 50 % of the width averaged. One line =
1/600 in = 0.04233 mm.

| aperture | start (line) | end (line) | centre | length (mm) | crossbar after (mm) | pitch to next (mm) | pitch (1/7200) |
|---|---|---|---|---|---|---|---|
| 1 |  600.14 | 1453.34 | 1026.74 | 36.119 | 1.900 | 38.005 | 10773.1 |
| 2 | 1498.74 | 2350.26 | 1924.50 | 36.047 | 1.924 | 37.927 | 10750.9 |
| 3 | 2395.72 | 3245.10 | 2820.41 | 35.957 | 1.974 | 37.851 | 10729.4 |
| 4 | 3291.72 | 4137.32 | 3714.52 | 35.797 | 1.968 | 37.765 | 10705.2 |
| 5 | 4183.80 | 5029.43 | 4606.62 | 35.799 | 1.996 | 37.825 | 10722.1 |
| 6 | 5076.59 | 5923.67 | 5500.13 | 35.860 |   --   |   --    |   --    |

Read off that:

- **There are exactly six apertures.** No more, no fewer, and the
  profile is unambiguous: the plastic reads ~780 counts, an open
  aperture ~39800, a film-filled one ~34500.
- **Aperture length 35.80-36.12 mm**, mean 35.92. A 35 mm frame is
  36 mm along the strip, so the holder's opening is the frame, with no
  slack to speak of.
- **Crossbars 1.90-2.00 mm.**
- **Pitch 37.77-38.01 mm**, mean 37.875, standard deviation 0.09 mm.
  Fitted against a constant-pitch model, the largest residual is 3.1
  lines = 0.13 mm. The holder is regular.

The pitch figures drift slightly downwards along the strip (38.005 ->
37.765 mm). Whether that is a real taper in the plastic or a slow drift
in the scan pass's line spacing cannot be separated from a single sweep,
and at 0.24 mm end to end it does not change any decision below.

Ahead of aperture 1 the profile shows a short lit window (lines
170-309, 5.93 mm) between two opaque runs. That is the identification
tab -- the hole that, per Plustek's own material, tells the scanner
which holder is inserted. It is optically visible; whether the firmware
reports it through a register is a separate question (section 9).

## 3. The pitch, from the vendor's own positioning commands

The apertures give the pitch in millimetres. The FEEDL grid gives it in
the units the motor actually takes. The vendor positions with a
mode-0x18 motor batch carrying FEEDL in registers 0x3d/0x3e/0x3f;
extracting every such write from the captures gives this.

**The nominal grid.** Before a vendor app scans, it runs a preview or
identify pass over the strip and positions on a fixed grid. That grid
is a constant step, identical in three independent captures made with
two different applications:

| capture | FEEDL series | steps |
|---|---|---|
| `20260829-session3-vackning` (QuickScan) | 6414, 17166, 27918, 38670 | 10752, 10752, 10752 |
| `segments/02-preview-magasin` | 6414, 17166 | 10752 |
| `20260905-vuescan-3600-ir` (preview pass) | 6414, 17166, 27918, 38670 | 10752, 10752, 10752 |

**Seven observed steps, every one of them exactly 10752.**

**The scan-pass values scatter, because they are film-detected.** The
values an app uses for the actual scan are not the grid: the app finds
the film's own frame edge first. That is why the base offset differs
from capture to capture (6414, 6548, 6716, 6743, 6746, 6776 have all
been seen) and why the steps wobble:

| capture | frame 1 | frame 3 | frame 4 | frame 5 | frame 6 |
|---|---|---|---|---|---|
| SilverFast `20260829-silverfast-ramval` | 6548 | 28052 | 38828 | -- | -- |
| VueScan final pass `20260905` | 6716 | 28232 | 38966 | -- | -- |
| WIA batch `segments/05-batch-komplett` | 6776 | 28274 | 39026 | **49796** | **60174** |
| this driver's own trace 03/04 | 6743 / 6746 | -- | -- | -- | -- |

Every one of those sits on a 10752 grid from its own base, to within
±30 steps (0.11 mm): SilverFast's frame 3 is `6548 + 2 x 10752` exactly;
the WIA batch's frames 3, 4 and 5 are within 12 steps of
`6776 + (n-1) x 10752`; and the WIA batch's frame 6, 60174, is
`6414 + 5 x 10752` **exactly** -- the app fell back to the untouched
nominal grid there, because position 6 held no film to detect.

**Conclusion: the holder's frame pitch is 10752 steps of 1/7200 inch =
37.947 mm, constant across all six positions.** It is a linear model
with a constant pitch; no position table is needed. The 0.09 mm
scatter in the optical pitch measurement is measurement noise plus the
scan pass's own scale error, not holder irregularity.

## 4. Frames 5 and 6 are inside the transport's demonstrated envelope

Two independent facts:

- The WIA batch capture drove this unit to frame 5 (FEEDL 49796) and
  frame 6 (FEEDL 60174) and scanned both, on 2026-08-29. Those are not
  extrapolations; they are commands this scanner executed.
- The longest move the transport makes is the load flow's traverse,
  FEEDL 71490 (~252 mm), issued on **every** load. Frame 6 at 60546 is
  10944 steps (38.6 mm) short of it.

So driving to frames 5 and 6 is not a new mechanical excursion. It is
inside travel the machine covers routinely.

## 5. The model the driver uses, and where it differs

`FEEDL_PITCH` in the table modules is 10760, not 10752. It comes from
the pass-3 reading of the SilverFast trace, which took
`frame 4 - frame 1 = 32280 = 3 x 10760`. That frame-4 value (38828) is
one of the film-detected ones, 24 steps off the grid; the same trace's
frame 3 sits exactly on the 10752 grid. The 10760 is an artefact of
reading a detected value as a nominal one.

The difference, per frame, with the driver's own base:

| frame | pitch 10760 (current) | pitch 10752 (measured) | difference |
|---|---|---|---|
| 1 |  6743 |  6743 | 0 mm |
| 2 | 17503 | 17495 | 0.028 mm |
| 3 | 28263 | 28247 | 0.056 mm |
| 4 | 39023 | 38999 | 0.085 mm |
| 5 | 49783 | 49751 | 0.113 mm |
| 6 | 60543 | 60503 | 0.141 mm |

**This is a decision, not a fix, and it has not been made.** Frames 1-4
are hardware-verified with 10760 and land correctly; the error there is
at most 0.085 mm. Changing the constant changes the wire for every
frame above 1, including three that are verified. The empty-holder
measurement in section 8 settles it directly: with a wrong pitch the
per-frame registration error grows linearly along the strip, at 8 steps
(0.028 mm) per frame, and that trend is exactly what the measurement
reads out. Nothing is changed before it does.

## 6. How much margin there actually is

The scan window is longer than the aperture, so a correctly positioned
frame includes the whole opening plus a sliver of crossbar at each end.
That sliver is the margin, and it is also a free registration reference:
**every delivered frame contains its own aperture edges**, so
registration can be read straight out of the image with no external
target.

The number that counts is the **delivered** height, not the height on
the wire: the colour-line correction crops the wrap artefact off both
ends (`image.align_channels`, and the same crop in the backend), so the
image is shorter than the scan.

| profile | wire lines | delivered lines | delivered (mm) | margin per side vs mean aperture 35.92 mm |
|---|---|---|---|---|
| plain 3600      |  5137 |  5113 | 36.077 | 0.08 mm |
| dual 3600 (IR)  | 10622 |  5248 | 37.028 | 0.55 mm |
| dual 600        |  1764 |   878 | 37.173 | 0.63 mm |
| dual 1200       |  3552 |  1768 | 37.422 | 0.75 mm |
| dual 2400       |  7088 |  3528 | 37.338 | 0.71 mm |
| dual 7200       | 21248 | 10576 | 37.311 | 0.70 mm |

Plain 3600 is the tight one, and tighter than it first looks: 0.08 mm
per side against the mean aperture, and **−0.02 mm** against the longest
aperture (frame 1, 36.119 mm) -- that is, at 3600 dpi in colour the
delivered image is a hair shorter than aperture 1 and cannot contain the
whole opening however well it is positioned. Frames 1-4 are verified to
look right at that resolution, so the missing sliver is at the very edge
of the film area and does not matter in practice; it does mean plain
3600 has no margin left to spend on a positioning error. The dual
profiles have 0.55-0.75 mm per side and are comfortable everywhere,
which is why the empty-holder run below is at 600 dpi.

Two numbers to compare that against:

- observed load-to-load geometry variation: **±4 lines at 3600 dpi =
  ±0.028 mm** (Test 17-23, ten repeats);
- the pitch question of section 5: **up to 0.14 mm at frame 6**.

So load-to-load repeatability is not the limiting factor. The pitch is,
and at plain 3600 dpi it is larger than the margin -- which is the
concrete reason the pitch has to be settled before frames 5 and 6 are
promised at that resolution.

## 7. Three things that must not be confused

- **A -- motor position.** Where the transport stops. Set by FEEDL.
- **B -- the holder's aperture.** Where the physical opening is. Fixed
  plastic, measured in section 2.
- **C -- the film frame.** Where the photographed image happens to sit
  inside the aperture. Depends on how the roll was shot and cut, and
  varies by a millimetre or so between strips.

The empty-holder test measures **A against B** and nothing else. The
film tests then measure **C against A/B**. This separation is the whole
point of running the empty holder: a frame that looks off-centre with
film in it may be a correctly positioned scan of a badly cut strip, and
without B measured there is no way to tell those apart. Do not move the
transport to chase C.

## 8. What still needs hardware

`NEEDS HARDWARE`, all of it. The measurement is the same one as section
2, but on our own driver's per-frame scans instead of the vendor's
sweep, and it reads the registration error directly:

For each frame 1-6, scan the **empty** holder and run

    tools/holder_geometry.py frame <raw> --width <w> --dpi <d> --dual

which reports `centre_offset_mm` -- the signed distance between the
aperture's measured centre and the scan window's centre. Six numbers.

Interpretation:

- A **constant** offset across all six frames is a base-offset
  (`FEEDL_FRAME1`) matter, not a pitch matter.
- A **linearly growing** offset is the pitch. The slope says which
  value is right: 10760 too large predicts about -0.028 mm per frame
  (0.14 mm by frame 6); 10752 predicts a flat line.
- **Scatter** across three separate loads is the load-to-load
  repeatability, to be compared with the ±0.028 mm already observed.

Definition of done for robust positioning, set before the tests are run:

1. All six scan windows contain their whole aperture, with the measured
   margin on both sides positive in every frame and every profile.
2. The residual after fitting a constant pitch is below the observed
   load-to-load variation, or the pitch constant is corrected so it is.
3. No unexplained systematic drift towards frame 6.
4. Frame 5 and 6 POSITION completes on the normal class-F status, inside
   its budget, with margin.
5. PARK completes normally from frames 5 and 6.
6. Frames 1-4 land where they landed before (no regression).
7. Frame 0 and frame 7 are refused before any write (already done
   offline; see section 10).

## 9. The identification tab and holder type

The leading tab's hole is visible in the strip profile (section 2), and
Plustek's material says the tab is how the holder is identified. No
register on this scanner has ever been observed to report a holder
type: `0x3b/0x3c` are the DPI-dependent base-register pair and the
eject guard's signature, and nothing else has been seen to change with
what is inserted.

So holder-dependent geometry cannot currently be selected automatically.
`of135i/holder.py` therefore names the strip holder as the assumed
default, and says so. If a read-only holder-type register is ever found,
the model is already the place to hang it off; until then, an unknown
holder must be treated as the strip holder or refused, not guessed at.

The mounted-slide holder that ships in the same box has four openings by
inspection of the part. Nothing about it has been captured, measured or
loaded: no FEEDL, no pitch, no aperture geometry, no load behaviour. It
is `NEEDS HARDWARE` in every respect, and its imaging is
`NEEDS ACTUAL SLIDE` on top of that. `of135i/holder.py` defines it with
a frame count and no geometry, so that nothing can position with it by
accident.

## 10. What changed in the driver from this analysis

Offline, no hardware:

- `of135i/holder.py` -- the holder model: frame count, measured
  geometry, and the two guards.
- Every `feedl_for_frame()` (six table modules) refuses a frame the
  holder does not have, before returning a target. Previously the
  Python driver had **no upper bound at all**: `--frame 99` computed
  FEEDL 1054023 and would have commanded it.
- `holder.check_feedl()` runs before both POSITION phases, refusing any
  target above 71490 -- an independent second guard that catches a bad
  FEEDL however it was produced.
- The CLI validates `--frame` and `--frames` before opening the device,
  and `digitize` takes `--frames` (default `1-4`, so a shorter strip is
  never scanned as six).
- The strip holder's frame count is 6, so frames 5 and 6 are reachable.
  They are **not** hardware-verified; section 8 is what verifies them.
