# v0.1.2 — IR channel alignment fix

Release notes for the `v0.1.2` tag (2026-09-12). Bug-fix release of the
standalone **Python / pyusb command-line driver**; scope otherwise as
[v0.1.1](release-notes-v0.1.1.md). As before, the software is **unofficial**
— not affiliated with, endorsed by, or supported by Plustek — and was
developed and tested by one person on a single OpticFilm 135i unit.

## What changed

**The infrared channel is now colour-line aligned** (`of135i.image.split_ir`).

The scanner's CCD reads red, green and blue on three physically separate
lines, so in raw data the three channels are staggered along the film strip
by 24·dpi/7200 lines (12 lines at 3600 dpi). The driver has always corrected
this for the visible image (`align_channels`). For the infrared pass the
driver assumed — following an early analysis note that read "R ≈ G ≈ B" —
that the three channel slots carried one and the same IR reading, and simply
averaged them. That note described the channels' *levels*, which are indeed
near-equal under IR light; their *positions* are staggered exactly like the
visible channels (measured −12 / +12 lines at 3600 dpi on the vendor's own
capture, on the driver's captures and on the SANE backend's — a 2-D
cross-correlation on dust, 2026-09-12). Averaging them unaligned turned every
dust speck into a triplet in the IR image and widened the dust mask
accordingly.

Since v0.1.2 `split_ir` rolls the R and B channels into register with G
before averaging — the same roll `align_channels` applies to the visible
image — so the IR image shows every speck once, at its true position, and
the dust mask is tighter. The wrapped edge rows are exactly the rows the CLI
already crops from both images, so **file geometry, registration between the
visible and IR products, and every other output are unchanged**. `--ir`
scans made with v0.1.1 or earlier are not wrong in the visible product; only
their `-ir.tiff` (and the dust mask derived from it) carried the triple
ghost, so re-running dust removal on such scans needs a rescan.

Other: the IR analysis note (`docs/ir-analysis.md`) is corrected; a synthetic
regression test (`tests/test_ir.py`) checks that a staggered synthetic IR
line comes out as a single speck at the G position.

## Verification

- Offline against the vendor's own IR capture: channel stagger −12 / +12
  before, the averaged image's 12-line autocorrelation peak gone after.
- Hardware: HARDWARE_VERIFICATION_TBD

## Everything else

Unchanged from v0.1.1: install, udev rule, workflow, the raw-negative product
and the preview positive, the aperture-registered production contract, the
known limitations and the single-unit test base. The SANE backend work on
`master` is separate from this tag and not part of the driver release.
