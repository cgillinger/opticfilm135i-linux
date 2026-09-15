# Contributing

This is a personal hobby project — a reverse-engineered Linux driver for the
Plustek OpticFilm 135i — maintained by one person, on a single scanner, in
spare time. Contributions are welcome, with a few honest expectations.

- **No promises on response time.** I may be slow, and I may decline changes
  that widen the scope beyond what I can test and maintain on one unit.
- **Keep the offline checks green.** They run automatically on every pull
  request, and locally via `tools/release_check.py`; a red run needs fixing
  before a change can go in. The checks never touch a scanner — see
  [`docs/offline-checks.md`](docs/offline-checks.md).
- **Hardware-affecting changes need real care.** The motor sequences, wait
  conditions and calibration were verified against the vendor application on
  my unit, and this hardware has stalled when driven from an undefined state.
  I can't take changes to them without evidence — describe what you tested,
  and on what.
- **Licensing.** By contributing you agree your changes are provided under the
  project's licenses (see [`LICENSING.md`](LICENSING.md)): the driver under
  GPL-2.0-or-later OR MIT, the SANE backend under GPL-2.0-or-later, and the
  documentation under CC BY 4.0.

If you got the driver working on another unit, an issue saying so is genuinely
valuable — cross-unit behaviour is otherwise unknown.
