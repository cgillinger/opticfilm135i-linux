# Licensing

This project is multi-licensed by area. **Every part requires attribution.**

| Area | Licence | What it means |
|---|---|---|
| **Standalone driver** — `of135i/`, `tools/`, the Python tests (`tests/*.py`) | **GPL-2.0-or-later OR MIT**, at your option | Christian Gillinger's own reverse-engineering work, with no GPL dependency, so it is offered under both. Use it under GPL to combine with GPL software, or under MIT ([`LICENSE-MIT`](LICENSE-MIT)) to include it in a proprietary product. MIT still requires the copyright notice to be preserved — that is the attribution. |
| **SANE backend** — `sane/`, the C++ probes (`tests/*.cpp`) | **GPL-2.0-or-later** ([`LICENSE`](LICENSE)) | Derived from and built against sane-backends, which is GPL, so this part cannot be relicensed. |
| **Documentation** — `docs/` | **CC BY 4.0** ([`docs/LICENSE`](docs/LICENSE)) | Free to use, including commercially, with attribution to Christian Gillinger. The protocol facts themselves are not copyrightable and are free regardless; attribution is requested. |
| Everything else (build/config) | **GPL-2.0-or-later** | The project default. |

Copyright (c) 2026 Christian Gillinger.

SPDX identifiers used: `GPL-2.0-or-later`, `MIT`, `CC-BY-4.0`.

## For a proprietary scanning application

The documentation (CC BY 4.0) and the protocol facts are usable directly, and
the standalone driver code may be used under MIT — in both cases with credit to
Christian Gillinger. The `sane/` backend is GPL; treat it as a reference
implementation, not as something to include in a closed-source product.
