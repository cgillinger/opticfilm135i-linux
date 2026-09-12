# v0.1.2 — IR channel alignment fix

Release notes for the `v0.1.2` tag (2026-09-12, commit `695862a`) of the
standalone **Python / pyusb command-line driver** for the Plustek OpticFilm
135i. As before, the software is **unofficial** — not affiliated with,
endorsed by, or supported by Plustek — and was developed and tested by one
person on a single OpticFilm 135i unit. Previous release notes:
[v0.1.1](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/docs/release-notes-v0.1.1.md).

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
already crops from both images, so the **file geometry** (dimensions, crop,
orientation, resolution tags) and the **registration** between the visible
and IR products are unchanged.

What this fix does and does not change in the outputs:

- The **visible data before dust removal** — the raw negative as captured,
  aligned and cropped — is not touched by this change. A `--no-clean` scan,
  a non-IR scan, and the `.overscan` visible frame are exactly what the same
  driver without this fix writes from the same raw buffer.
- The **`-ir.tiff`** product changes: single specks instead of triplets.
- Consequently the **IR-based dust removal** (`remove_dust`, on by default
  with `--ir`) works from a different dust mask, so the automatically
  cleaned visible negative — and the `--positive` preview derived from it —
  **can differ** from v0.1.1's output in the cleaned areas. Preserved
  geometry does not mean identical image content.

**Scans made with v0.1.1 or earlier.** Their visible product is not wrong;
only their `-ir.tiff` (and the dust mask derived from it) carried the triple
ghost. Whether such a scan can be corrected without rescanning depends on
what was kept: the alignment needs the three IR channel samples *separately*,
which exist only in the original complete raw capture (the alternating-line
buffer the driver reads from the device). An already averaged `-ir.tiff`
does not carry that information, and the driver's normal `scan`/`digitize`
outputs are the averaged products, not the raw buffer. So a scan whose
complete raw capture was kept can in principle be re-processed offline with
the corrected `split_ir`; a scan for which only the TIFF products remain
needs a rescan for a corrected IR image and dust mask. The driver does not
ship a re-processing command for archived raw captures.

Other: the IR analysis note
([docs/ir-analysis.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/docs/ir-analysis.md))
is corrected; a synthetic regression test
([tests/test_ir.py](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/tests/test_ir.py))
checks that a staggered synthetic IR line comes out as a single speck at the
G position.

## Verification

- Offline against the vendor's own IR capture: channel stagger −12 / +12
  before, the averaged image's 12-line autocorrelation peak gone after.
- Hardware (2026-09-12, the developer's unit, Test 73 in
  [docs/test-log.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/docs/test-log.md)):
  one `scan --frame 1 --dpi 3600 --ir` with v0.1.2 — normal transport,
  aperture coverage verified (margins 0.91 / 0.62 mm), calibration as every
  earlier run; the `-ir.tiff` has no 12-line ghost (strip-axis
  autocorrelation 0.60 at 12 lines against 0.63 / 0.61 at 6 / 18, where the
  2026-09-10 product made with the old code shows 0.83 against 0.80 / 0.80).
  Single specks in the review crop.

## Scope of the tag

The **declared functional scope** of this release is the Python / pyusb
command-line driver (`of135i/`, `tools/`, the udev rule), as for v0.1.1.

The tag is a snapshot of `master`, so it also contains the in-progress
**SANE backend sources** (`sane/`: the GL126 command set, register tables
and the genesys integration patch). That code is present in the tag but has
its own, separate development and verification status — tracked in
[docs/ROADMAP.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/docs/ROADMAP.md)
and
[docs/sane-port.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/docs/sane-port.md)
— and is not covered by this release's claims. Its hardware runs are logged
per profile in the test log; do not read the driver's verification as the
backend's.

## Driver changes since v0.1.1

v0.1.1 was tagged on 2026-09-06. Besides the IR fix above, the driver as
tagged at v0.1.2 carries the work that landed on `master` in between (the
geometry work hardware-verified on the developer's unit, Tests 55–61):

- **Six-frame strips**: frames 1–6 (the strip holder's measured geometry),
  with a hard upper bound on the frame number (`a029cd5`).
- **Overscan with aperture-registered crop as the production default** for
  plain 3600 dpi and all dual-light profiles: the driver scans a window with
  a margin around the holder aperture, detects the aperture edges in the
  scan, and writes the product only when coverage verifies; the full overscan
  frame is always preserved (`c709083`, `6d56fe6`, `2b19c7f`; Tests 57–61).
  The corrected frame-position constants replace the ones v0.1.1 used.
- **Honest TIFF resolution tags for the anisotropic 2400 dpi profile** (per
  axis), and the IR TIFF's axes follow the delivered orientation (`0fe3506`,
  `1fb1144`) — metadata only, pixels unchanged, verified offline.
- `load --release` / `load --double-jog` for the power-cycled, latched
  magazine start (`0389df2`), and a human explanation when the load feed does
  not engage (`5265ddd`).
- The scan output directory is validated before any hardware write
  (`c855061`); `eject` refuses the base-table-only device state read-only
  (`a943756`).

Unchanged from v0.1.1: the install steps and the udev rule, the raw-negative
product and the preview positive as the two output kinds, and the single-unit
test base. The README's usage and limitations sections at the tag
([README](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.1.2/README.md))
describe the driver as tagged.
