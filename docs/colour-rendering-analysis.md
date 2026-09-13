# Why our positives come out yellow — the orange mask, measured

Status: **diagnosed 2026-09-13, not yet fixed.** The mechanism is
established and the measurement that a fix needs is demonstrated on real
data. No code has changed. This is an application-layer problem: the raw
data is sound.

Nothing in this document is vendor-derived. The comparison against
Plustek's own rendering is kept in the private analysis area, per the
project's standing rule that conclusions may travel but vendor files may
not.

## 1. The symptom

Every positive this project has rendered from a colour negative has a
yellow-green cast. It has been visible since the first accepted
production set (Test 58/N3, 2026-09-10), it was re-reported on the WP-4
frame 1 of 2026-09-13, and the colour verdict has been parked on "compare
against a vendor scan of the same strip" ever since.

The explanation carried until now was: *`to_positive` stretches each
channel over its own range, and since red spans more density than green
and blue, the stretch pushes red down.* That explanation is **wrong**,
and it was disproved by measurement, not by argument.

## 2. What actually happens

Take frame 1 of 2026-09-13, invert the negative, stretch each channel to
full range on its own 0.5/99.5 percentiles — exactly what the preview
renderer does — and look at where the values land:

| channel | min | median | mean | max |
|---|---|---|---|---|
| R | 0.0 | 191.0 | 173.4 | 255.0 |
| G | 0.0 | 203.9 | 187.4 | 255.0 |
| B | 0.0 | **77.0** | 93.7 | 255.0 |

Every channel reaches both ends. The endpoints are not the problem —
per-channel full-range normalisation is doing exactly what it promises.
But blue's **median sits 127 code values below green's**. Low blue is
yellow; low blue with slightly low red is yellow-green. That is the cast,
and it lives entirely in the midtones.

So the failure is not the stretch. It is that **the three channels'
distributions have different shapes**, and matching them at the endpoints
leaves their middles far apart. A linear stretch cannot fix that, and no
choice of percentile will.

## 3. The cause: the orange mask, never removed

A colour negative has an orange mask — a coloured film base that
transmits red and absorbs blue. It is not part of the image; it is a
constant offset the image sits on top of. Standard negative processing
removes it before inverting. Ours never did.

**It is measurable in our own data.** The scan window includes an
overscan margin beyond the frame (the A+C geometry,
`docs/holder-position-design.md`), and unexposed film is inside it.
Profiling frame 1 from top to bottom:

- the uniform bands at the extreme ends are dark and nearly neutral
  (R 640 / G 780 / B 730) — that is the holder's **plastic aperture
  edge**, not film;
- between the image and the plastic, rows 5183–5199, is a band that is
  bright and strongly red-weighted. That is **film base**.

Measured there — 16 rows, about 0.11 mm at 3600 dpi, medians over the
central 2900 columns:

| channel | value (16-bit) | 8-bit equivalent | density |
|---|---|---|---|
| R | 35591 | 138.5 | 0.265 |
| G | 13646 | 53.1 | 0.681 |
| B | 9514 | 37.0 | 0.838 |

R/B = 3.74. That is the orange mask, unmistakably: transmits red,
absorbs blue, and the density difference between blue and red is 0.573.

Now compare that with the stretch result in §2. Blue starts 0.573
density units darker than red on the negative. Invert without removing
that offset, stretch to full range, and blue's midtones land low. The
number in §2 and the number here are the same fact seen twice.

## 4. Why the measurement is trustworthy

The obvious worry is that our own calibration already absorbs part of the
mask, which would make this measurement circular. It does not, and the
project had already proved it without meaning to.

Our AFE gain calibration reads a white reference and picks per-channel
gains. On 2026-09-13 the white peaks were R 22206, G 31626, B 25911 and
the codes chosen were R 0x2e, G 0x20, B 0x27 — the highest gain to the
weakest channel, equalising white. The sensor and lamp are strongly
green-favouring: red sits 0.154 density units below green before
correction.

Crucially, that white reference is read **before the film**, and **Test
60 demonstrated the consequence empirically**: a black-and-white silver
strip and a colour negative, scanned in the same session, produced
*identical* gain codes (46/32/39). Gain is film-independent as a matter
of measurement, not just of design.

So if the gain stage had been absorbing the mask, the mask would have
normalised toward zero density and §3 would have found nothing. It found
a strong mask against a film-independent reference. The measurement
stands on its own.

## 5. The fix, and why it belongs to us rather than to constants

The mechanism points at one change: **measure the mask from unexposed
film, subtract it per channel, then invert and render.** Two properties
make this the right shape:

- it is **per strip and per scan**, so it adapts to film stock, age and
  exposure, none of which a fixed constant can know;
- it needs **no new hardware behaviour**. The mask is already in every
  scan we take.

Sampling sites, best first:

1. **The inter-frame gap.** The holder's own geometry puts about 1.90 mm
   of unexposed film between frames — wide, clean, and reachable by
   positioning deliberately.
2. **The overscan margin**, which is what §3 used. At the default
   0.75 mm overscan only about 0.11 mm of film base is exposed before the
   plastic edge begins. Enough to prove the method; thin for production.

The alternative — hardcoding per-channel black and white points — is
what consumer scanner software does. It works when one vendor pairs known
constants with a known scanner and a profile tuned to the combination. We
have the better option available and should take it: a measurement beats
a calibration when the measurement is free.

## 6. What this does NOT touch

The raw data is fine. Frame 1 reaches full range in all three channels
with **0.0000 % clipped high and 0.0000 % clipped low**, measured 16 bits
per channel. The sensor path, the calibration, the geometry and the
transport are not implicated by anything here.

This is `to_positive` and the preview renderer — the application layer,
which is exactly where this project's standing principle puts colour:
*the driver delivers correct raw data; colour interpretation is the
application's job* (Christian, 2026-09-05). The finding does not change
that principle. It says our own preview has been a poor application of
it.

## 7. Open, and deliberately so

- The fix is **not implemented**. Nothing in `of135i/image.py` or
  `to_positive` has changed.
- Whether the driver should ship mask compensation at all, or leave it to
  the frontend, is Christian's decision. The argument for shipping it: a
  preview that is obviously wrong is worse than no preview. The argument
  against: it is interpretation, and interpretation is the application's.
- A second strip, ideally a different film stock, is needed before the
  method can be called general. One strip demonstrates; it does not
  generalise.
