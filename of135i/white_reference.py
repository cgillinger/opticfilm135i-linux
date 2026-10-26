"""Clean white-reference profiles, kept between runs.

The calibration's white measurement is taken in front of aperture 1, where a
110 strip can reach; calibrate.guard_white_blockage fills the columns the
film blocks. It fills best from a CLEAN profile of the same kind (the lamp's
and sensor's lateral shape), scaled by the measurement itself on the clear
columns either side. This module keeps the newest clean profile per kind on
disk: every calibration whose white measurement has no blockage stores its
profile, so the reference follows the unit and the lamp.

One small .npy per kind, `white-<kind>.npy` ((width, 3) float32), written
atomically (temporary file + os.replace) under the state directory:
$OF135I_STATE_DIR, else $XDG_STATE_HOME/of135i, else ~/.local/state/of135i.
A store problem never fails a scan: reads that fail give None, writes that
fail are logged.

Only the CLI's real scanner gets a store (Scanner(white_reference=...));
a Scanner built without one (tests, tools) neither reads nor writes it.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def default_dir() -> Path:
    env = os.environ.get("OF135I_STATE_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "of135i"


def kind_key(kind: str, dpi: int, width: int) -> str:
    """'plain' / 'dual-vis' / 'dual-ir' plus the profile it was measured in."""
    return f"{kind}-{int(dpi)}-{int(width)}"


class WhiteReferenceStore:
    def __init__(self, directory: "str | Path | None" = None):
        self.dir = Path(directory) if directory is not None else default_dir()

    def _path(self, key: str) -> Path:
        return self.dir / f"white-{key}.npy"

    def load(self, key: str) -> "np.ndarray | None":
        p = self._path(key)
        try:
            arr = np.load(p, allow_pickle=False)
        except FileNotFoundError:
            return None
        except Exception as e:                 # a damaged file is no reference
            log.warning("white reference %s unreadable (%r); ignored", p, e)
            return None
        if arr.ndim != 2 or arr.shape[1] != 3 or not np.all(np.isfinite(arr)):
            return None
        return arr.astype(np.float64)

    def save(self, key: str, profile: np.ndarray) -> bool:
        prof = np.asarray(profile, dtype=np.float32)
        if prof.ndim != 2 or prof.shape[1] != 3 or not np.all(np.isfinite(prof)):
            return False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".white-", suffix=".npy", dir=self.dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    np.save(f, prof, allow_pickle=False)
                os.replace(tmp, self._path(key))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            log.warning("white reference %s not saved (%r)", self._path(key), e)
            return False
        return True


def guard(store: "WhiteReferenceStore | None", key: str, white_meas):
    """calibrate.guard_white_blockage with the stored reference of `key`;
    a measurement without blockage becomes the new reference. Returns
    ``(guarded, blocks)``."""
    from . import calibrate
    ref = store.load(key) if store is not None else None
    guarded, blocks = calibrate.guard_white_blockage(white_meas, ref)
    if blocks:
        log.info("white measurement %s: blocked column range(s) filled: %s",
                 key, [b.as_dict() for b in blocks])
    elif store is not None:
        store.save(key, np.asarray(white_meas, dtype=np.float64).mean(axis=0))
    return guarded, blocks
