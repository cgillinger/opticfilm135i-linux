# The colour cast in our preview positives — what is measured, and what is not

Status: **PARKED 2026-09-15 (Christian's decision), and no colour change
has been implemented.** The vendor's own software renders the same strip
*more* strongly yellow than we do, and a different stock renders
neutrally through the vendor's unchanged settings (§8); the scanner
itself is unchanged by the project's work (Test 82). The cast varies
between strips, appears in the vendor path too, and has not been shown
to be a defect in our code; the film's properties and the chosen
treatment remain possible explanations. Post-processing is outside this
project's scope.
An earlier revision of this document
(2026-09-13, commit `0aab922`) claimed the cast was caused by the orange
mask never being removed. **That claim was wrong** and is retracted in
§5, which explains why. What survives is a set of measurements, kept here
because they are real and reproducible, and a much smaller set of
conclusions.

Nothing here is vendor-derived. Comparison material from the vendor's own
software is kept in the private analysis area, per the project's standing
rule that conclusions may travel but vendor files may not.

## 1. The symptom

Positives rendered from this colour negative look yellow-green. Christian
has reported it on the accepted production set (Test 58/N3, 2026-09-10)
and again on the WP-4 frame of 2026-09-13.

Measured on frame 1 of 2026-09-13 (`wp4-f1.tiff`), as channel means on an
8-bit scale. The last two rows are the vendor's own software rendering
**the same strip**, obtained the same evening (§8):

| renderer | R − G | B − G | median B |
|---|---|---|---|
| `of135i.image.to_positive()` | −26.0 | −66.9 | 98 |
| an ad-hoc linear-inversion preview (§4) | −14.0 | −93.6 | 76 |
| Plustek QuickScan, JPEG, same strip | −0.9 | −115.4 | 22 |
| Plustek QuickScan, TIFF, same strip | −1.3 | −111.3 | 20 |
| Plustek QuickScan, JPEG, *different* strip (2026-08-29) | −1.0 | −11.7 | 149 |

Blue is deficient in our renderings. Low blue is yellow. The symptom is
real and it is in our output. **It is also, and more strongly, in the
vendor's output for this strip** — which changes what the symptom means.
See §8.

## 2. What `to_positive()` actually does

Read the code before theorising about it (`of135i/image.py`):

1. `base` = the **99.8th percentile per channel** — the unexposed film
   base, which is the brightest thing in a negative;
2. `dens = log10(base / px)` — density measured **from that base**;
3. per-channel normalisation of the density to its own 0.5/99.5
   percentiles;
4. a print gamma.

Two consequences follow directly, and both matter:

- **The orange mask is already removed, by construction.** Density is
  measured relative to the per-channel film base, which *is* the mask
  level. The docstring has said so since the function was written.
- **A constant per-channel offset in density cannot survive step 3
  anyway.** Per-channel percentile normalisation removes offset *and*
  span differences. Even a badly measured `base` would cancel.

So no explanation of the cast can rest on "the mask is not removed", and
none can rest on "red spans more density than green and blue" either —
step 3 normalises span away too.

## 3. What is left, stated as description rather than diagnosis

After per-channel normalisation every channel occupies the full range, so
the only thing that can still differ is **the shape of each channel's
distribution within that range** — where the mass sits between the
endpoints. Measured on frame 1 with the driver's own function, the
channel medians land at R 155, G 186, B 99 on an 8-bit scale.

That is a restatement of the symptom in more precise terms. **It is not a
cause.** Whether those shapes differ because of the film (age, stock,
exposure), the scanner's channel response, the choice of percentiles, the
gamma, or something else, is not established by anything measured so far.

## 4. Which code produced which reviewed image

Traceability matters here, because the previous revision of this document
generalised from the wrong renderer.

- **2026-09-13, `wp4-f1-preview.png`** — the image Christian reviewed and
  called yellow that evening — was produced by an **ad-hoc script**, not
  by the driver: linear inversion (`65535 - x`), per-channel 0.5/99.5
  percentile stretch, rotate 90°. It does **not** use the density domain
  and does **not** reference the film base. Preserved as
  `plustek-135i-analys/wp4-20260913/render-scripts/look.py`.
- **2026-09-13, `wp4-f1-to_positive.png`** — the driver's real
  `to_positive()` on the same frame, rendered afterwards for this
  document. Script: `render-scripts/tp.py`.
- **2026-09-10, the N3 set** — `to_positive()` plus a "v2" variant with a
  per-channel gamma anchored on the median density. The script was **not
  preserved** and the chain cannot be reconstructed exactly; only the
  output images and the test-log description remain.

## 5. The retracted claim

The previous revision argued: the endpoints are not the problem because
every channel reaches both ends; therefore the cause is the orange mask,
which is never removed; therefore the fix is to measure the mask per
strip and subtract it.

The first step is sound. **The second does not follow, and is false for
`to_positive()`**, which already references the film base per channel and
then normalises per channel — so the mask is removed twice over, and a
constant error in measuring it would cancel regardless.

The error was generalising from the ad-hoc linear-inversion preview of
§4, which genuinely does not handle the mask, to the driver's function,
which does. They are different renderers and only one of them was
measured before the conclusion was written.

The proposed fix therefore rests on nothing: measuring the film base more
carefully would change `base` slightly, and step 3 would then remove the
difference. **No colour change should be implemented on this reasoning.**

## 6. Observations worth keeping

These stand on their own and are reproducible.

**The film base is directly measurable in our own scans.** The A+C
overscan margin contains unexposed film. On frame 1, rows 5183–5199
(about 0.11 mm at 3600 dpi, between the image and the holder's plastic
edge, which is dark and near-neutral at R 640 / G 780 / B 730):

| channel | value (16-bit) | density |
|---|---|---|
| R | 35591 | 0.265 |
| G | 13646 | 0.681 |
| B | 9514 | 0.838 |

R/B = 3.74 — transmits red, absorbs blue, as an orange mask should. The
holder's own geometry offers about 1.90 mm of unexposed film between
frames, a far wider sampling site than the 0.11 mm the default 0.75 mm
overscan exposes. Useful if a future design ever needs an explicit mask
measurement; it does not diagnose the cast.

**Our gain calibration does not see the film.** The AFE white reference
is read before the film, and Test 60 demonstrated the consequence
empirically: a silver black-and-white strip and a colour negative scanned
in one session produced identical gain codes (46/32/39). So per-channel
gain is film-independent as a matter of measurement. This rules out one
possible confound; it does not explain the cast.

**The raw data does not clip.** Frame 1 reaches full range in all three
channels with 0.0000 % at either end, measured at 16 bits per channel.
This says the signal is not being destroyed by the capture path. **It
does not say the colour calibration is correct** — an uncorrected or
mis-scaled channel can occupy the full range perfectly well. The two
questions are independent.

## 7. What would actually settle it

None of this is scheduled; it is what a real diagnosis would need.

1. **A second strip, ideally a different stock and a newer one.** The
   present strip was chosen for the holder tests because it has six
   frames, not for its colour. If a fresh strip renders neutrally
   through the same code, the cast belongs to this film.
2. **A known-good reference rendering of the same raw file** — darktable's
   negadoctor, or another established negative pipeline — to separate
   "our renderer is wrong" from "this negative is like that".
3. ~~**A vendor rendering of the same strip.**~~ **Obtained 2026-09-13.**
   It does not support the idea that our renderer is at fault; see §8.

Items 1 and 3 are now answered: a second strip of a different stock
(Kodak Gold 200) renders neutrally through the vendor path, and the
vendor rendering of the original strip exists. Item 2 is the only one
left, and it is no longer needed to exonerate our code — it would only
characterise the first strip more precisely.

## 8. The vendor reference, and what it settles

Obtained 2026-09-13, after two failed attempts. It took three runs of the
same six-frame strip through Plustek QuickScan on the same evening,
changing exactly one output setting at a time, because the first two
attempts were misread.

**What was run.** 48-bit TIFF, then 24-bit TIFF, then 24-bit JPEG. Bit
depth and container were each held constant while the other changed. All
three carried identical capture settings, verified afterwards against the
application's saved `Pref.ini` rather than against anyone's memory of
what was clicked: negative mode, 3600 dpi, auto-exposure **off**, scratch
removal off, the non-IR ICC profile applied.

**The result: the output path makes no difference.** Per frame, blue's
channel mean moves by less than half a level between the 24-bit TIFF and
the 24-bit JPEG, and B − G moves by less than 5 parts in 170. The two
files are visually indistinguishable. Every output variable was held
constant in turn and the cast followed none of them.

**A measurement trap, recorded because it nearly cost a wrong
conclusion.** The *fraction of pixels at exactly zero* is not comparable
across containers. Frame 2 reads 53 % zeros as TIFF and 20 % as JPEG with
an identical channel mean, because the JPEG's DCT lifts clipped zeros to
small positive values. That statistic was the discriminator used through
most of the investigation and it is container-dependent. Channel means
and B − G are the figures that hold.

**What the reference actually says.** The vendor's own software renders
this strip with blue at a median of 22 on an 8-bit scale, against our 98.
Its B − G is about −160 averaged over the six frames, against our −66.9.
**Our renderer does not exaggerate the blue deficit; it understates it,
by a wide margin.** The images bear the numbers out on inspection: the
vendor's version is close to monochrome yellow-green, ours retains colour
in clothing and in the flowers.

**What it does not say.** Both renderings descend from the same raw
capture on the same scanner, so a deficiency in the scanner's blue
channel would appear in both. Two facts argue against that being the
explanation, without closing it:

- The gain codes for this strip are R 46 / G 32 / B 39, identical to
  every run since Test 58. Test 60 established that these are measured
  before the film and are film-independent, so the capture front end is
  behaving exactly as it did when neutral results were produced.
- A different strip, scanned on this scanner through this software on
  2026-08-29, renders neutrally (B − G −11.7, median blue 149).

**That last question was then settled, the same evening.** A four-frame
strip of Kodak Gold 200 was scanned through the **unchanged** vendor
settings, verified against `Pref.ini` beforehand, and it renders
neutrally:

| set | B − G across frames | median blue |
|---|---|---|
| strip 1, the six-frame strip | −115 to −172 | 20 |
| strip 2, Kodak Gold 200 | −6.6 to −32.0 | 136–150 |
| a third strip, 2026-08-29 | −11.7 | 149 |

Same software, same scanner, same evening, nothing touched between runs.
The vendor path works and the settings are not the fault. It also
disposes of the leading hypothesis about auto-exposure, which was off for
this run too.

**Conclusion, as far as the evidence carries.** The cast appears in the
vendor's path too, it varies between strips, and it has not been shown
to be a defect in our rendering code. What remains possible is the film
itself — an unusually dense or aged negative — and its interaction with
the chosen treatment (film profile, exposure), which this comparison did
not separate. Our renderer shows *less* of the cast on this material
than the vendor's does, which is the opposite of what was suspected when
this document was opened. (Revised 2026-09-15: an earlier wording said
the cast "belongs to the first strip itself", which is more than was
measured.)

**What this closes.** The colour question has been open since Test 58
(N3, 2026-09-10), where the eye acceptance passed but the verdict on
colour was parked pending a vendor comparison of the same strip. That
comparison now exists, and the verdict is that no colour change is
warranted on this evidence. The driver keeps delivering linear raw data
and `to_positive()` stays as it is. Test 82 (2026-09-15) added that the
scanner is unchanged by the project's work and that the dual/IR profile
is not the cause; the question was then parked as outside the driver's
scope.

**Provenance.** The vendor renderings are archived outside this
repository, per the standing rule that conclusions may travel but vendor
files may not. The measurements above were taken independently on the
Linux host and in the Windows VM, by different code paths, and agree.
