#!/usr/bin/env python3
"""Offline tests for of135i.film110_report -- the holder-language report,
the strip-end frame-position arithmetic and the joint next-pass planner.
Pure functions; plain asserts. Run with:
    .venv/bin/python tests/test_film110_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import film110_report as rep
from of135i.holder import FILM_110, STRIP

P = FILM_110.pitch_mm
L = FILM_110.image_mm[0]


def test_legend_is_one_line_built_from_the_one_constant():
    text = rep.legend()
    assert "\n" not in text and text.startswith("Holder: apertures 1-6 and bars 1-5")
    assert rep.APERTURE_1_END in text
    for word in ("towards aperture 1", "towards aperture 6", "end walls are at aperture 1 and aperture 6"):
        assert word in text, word
    assert "left" not in text and " in/out" not in text
    print("test_legend_is_one_line_built_from_the_one_constant OK")


def test_holder_geometry_words():
    spans = rep.plastic_spans()
    assert [(k, i) for k, i, _a, _b in spans] == (
        [("end_wall", 1)] + [("bar", k) for k in range(1, 6)] + [("end_wall", 6)])
    assert rep.aperture_span(1)[0] == 0.0
    assert abs(rep.aperture_span(2)[0] - STRIP.pitch_mm) < 1e-9
    # bar k is between aperture k and k+1
    assert abs(spans[1][2] - rep.aperture_span(1)[1]) < 1e-9
    assert abs(spans[1][3] - rep.aperture_span(2)[0]) < 1e-9
    # entries: whole = aperture; split = the bar / end wall that splits it
    assert rep.where_of_entry({"aperture": 3, "whole": True, "split": None}) == {"kind": "aperture", "index": 3}
    assert rep.where_of_entry({"aperture": 2, "whole": False, "split": "trailing"}) == {"kind": "bar", "index": 2}
    assert rep.where_of_entry({"aperture": 2, "whole": False, "split": "leading"}) == {"kind": "bar", "index": 1}
    assert rep.where_of_entry({"aperture": 1, "whole": False, "split": "leading"}) == {"kind": "end_wall", "index": 1}
    assert rep.where_of_entry({"aperture": 6, "whole": False, "split": "trailing"}) == {"kind": "end_wall", "index": 6}
    assert rep.where_text({"kind": "end_wall", "index": 1}) == "the end wall at aperture 1"
    assert rep.locate_window(3.5) == {"kind": "aperture", "index": 1}
    assert rep.locate_window(29.0) == {"kind": "bar", "index": 1}
    print("test_holder_geometry_words OK")


def test_amounts_are_quarter_images_with_mm_in_brackets():
    assert rep.amount_text(12.75) == "half an image (about 13 mm)"
    assert rep.amount_text(-12.75) == "half an image (about 13 mm)"
    assert rep.amount_text(6.4) == "a quarter of an image (about 6 mm)"
    assert rep.amount_text(19.1) == "three quarters of an image (about 19 mm)"
    assert rep.amount_text(25.5) == "one image (about 26 mm)"
    assert rep.amount_text(38.2) == "an image and a half (about 38 mm)"
    assert rep.quarter_images(12.75) == 0.5 and rep.quarter_images(-6.0) == 0.25
    print("test_amounts_are_quarter_images_with_mm_in_brackets OK")


def test_pass_report_is_ordered_along_the_holder_and_carries_outcomes():
    images = [
        {"number": 2, "aperture": 1, "whole": False, "split": "trailing", "free_end_near": False,
         "position_mm": 29.0, "main": None, "located_by": "holes"},
        {"number": 1, "aperture": 1, "whole": True, "split": None, "free_end_near": False,
         "position_mm": 3.5, "main": "x", "located_by": "holes+frame"},
        {"number": 4, "aperture": 3, "whole": True, "split": None, "free_end_near": True,
         "position_mm": 80.0, "main": "y", "kept": True, "located_by": "holes"},
    ]
    blanks = [{"number": 3, "aperture": 2, "position_mm": 54.5, "located_by": "frame"}]
    r = rep.build_pass_report(images, blanks)
    assert [x["number"] for x in r] == [1, 2, 3, 4]
    assert [x["state"] for x in r] == ["whole_supported", "split", "blank", "whole_free_end"]
    assert [x["outcome"] for x in r] == ["new", "not_delivered", "not_delivered", "kept"]
    assert r[1]["where"] == {"kind": "bar", "index": 1}
    assert r[0]["located_by"] == "holes+frame"
    lines = rep.format_pass_report(r)
    assert lines[0].startswith("  aperture 1: image 1 whole (delivered)"), lines
    assert any(ln.startswith("  bar 1: image 2 split, ") for ln in lines), lines
    assert any("frame position 3 is blank" in ln for ln in lines), lines
    print("test_pass_report_is_ordered_along_the_holder_and_carries_outcomes OK")


def test_strip_bounds_front_and_rear():
    # A strip seen as in pass 1: photographs 1..4 at 18.3, 43.8, 69.3, 94.8,
    # front end 3.06 (a 15 mm leader before photograph 1), rear end 115.
    s = [(1, 18.3), (2, 43.8), (3, 69.3), (4, 94.8)]
    assert rep.strip_bounds(s, (3.06, 115.0), False) == (1, 4)
    assert rep.strip_bounds(s, (None, None), False) == (None, None)
    # a window that reaches before the film's start by more than the
    # tolerance is not a frame position (the leader), one that fits is
    assert rep.strip_bounds(s, (-8.0, None), False)[0] == 0
    assert rep.strip_bounds(s, (-2.0, None), False)[0] == 1
    # the same strip turned: numbers fall along the holder, the front end is
    # at the HIGH side
    t = [(4, 3.0), (3, 28.5), (2, 54.0), (1, 79.5)]
    assert rep.strip_bounds(t, (None, 100.0), True)[0] == 1
    assert rep.strip_bounds(t, (-4.0, 100.0), True) == (1, 4)
    print("test_strip_bounds_front_and_rear OK")


def test_planner_slides_a_split_image_into_an_aperture():
    # image 2 starts at 29.0 mm: split by bar 1. One quarter or half an image
    # towards aperture 6 brings it inside aperture 2.
    known = {1: 3.5, 2: 29.0, 3: 54.5, 4: 80.0}
    nxt = rep.plan_next_pass(known, {2: "split"}, [], {}, {2: "now split by bar 1"})
    assert nxt is not None and nxt["rescues"] == [2]
    assert nxt["direction"] == "towards aperture 6" and nxt["shift_mm"] > 0
    assert nxt["images"] in (0.25, 0.5, 0.75)
    assert nxt["targets"][0]["number"] == 2
    assert nxt["text"].startswith("Next pass: move image 2 (now split by bar 1) ")
    assert "whole into an aperture" in nxt["text"] and " mm" not in nxt["text"], nxt["text"]
    assert "slide" not in nxt["text"] and "towards" not in nxt["text"], nxt["text"]
    # every known window and seen end stays inside the holder
    lo, hi = rep.holder_limits()
    for p in known.values():
        assert lo <= p + nxt["shift_mm"] and p + L + nxt["shift_mm"] <= hi
    assert nxt["remaining"] == [] and nxt["another_pass_after"] is False
    print("test_planner_slides_a_split_image_into_an_aperture OK")


def test_planner_never_pushes_the_strip_out_of_the_holder():
    # image 1 starts on the end wall at aperture 1; a move towards aperture 1
    # would leave the holder, so only moves towards aperture 6 are offered.
    known = {1: -1.0, 2: 24.5}
    nxt = rep.plan_next_pass(known, {2: "split"}, [], {}, {2: "now split by bar 1"})
    assert nxt is not None and nxt["shift_mm"] > 0, nxt
    # a strip pressed against the far end wall cannot go further
    far = 6 * STRIP.pitch_mm
    known = {3: far - L - 0.5, 4: far - L - 0.5 - P}
    nxt = rep.plan_next_pass(known, {4: "split"}, [], {}, {})
    assert nxt is None or nxt["shift_mm"] <= 0, nxt
    print("test_planner_never_pushes_the_strip_out_of_the_holder OK")


def test_planner_rescues_a_free_end_image_only_with_the_end_on_plastic():
    # photograph 4 whole in aperture 3 with the strip end 3 mm beyond it
    p4 = 2 * STRIP.pitch_mm + 8.0
    end = p4 + L + 3.0
    known = {4: p4}
    # the end lies in aperture 3; a quarter-image grid never puts a 2 mm bar
    # exactly there from this seating -> either no placement, or one whose
    # end really is on plastic
    nxt = rep.plan_next_pass(known, {4: "free_end"}, [end], {4: end}, {})
    if nxt is not None:
        e = end + nxt["shift_mm"]
        assert any(a <= e <= b for _k, _i, a, b in rep.plastic_spans()), (nxt, e)
    # with the end placed so that a quarter-image move lands it on bar 3
    bar3 = [(a, b) for k, i, a, b in rep.plastic_spans() if k == "bar" and i == 3][0]
    target_end = (bar3[0] + bar3[1]) / 2
    q = P / 4
    end2 = target_end - 2 * q          # two quarter moves from the bar's middle
    p4b = end2 - 3.0 - L
    nxt = rep.plan_next_pass({4: p4b}, {4: "free_end"}, [end2], {4: end2}, {})
    assert nxt is not None and nxt["rescues"] == [4], nxt
    e = end2 + nxt["shift_mm"]
    assert any(a <= e <= b for _k, _i, a, b in rep.plastic_spans()), (nxt, e)
    print("test_planner_rescues_a_free_end_image_only_with_the_end_on_plastic OK")


def test_planner_prefers_the_smaller_move_when_margins_are_comfortable():
    # two needed images that a ~15 mm move rescues together; a 50 mm move
    # would too, with a little more margin: the 15 mm one must win.
    known = {1: 3.5, 2: 29.0, 3: 54.5}
    nxt = rep.plan_next_pass(known, {2: "split", 3: "unseen"}, [], {}, {})
    assert nxt is not None
    assert abs(nxt["shift_mm"]) < 30, nxt
    print("test_planner_prefers_the_smaller_move_when_margins_are_comfortable OK")


def test_planner_shift_is_exact_and_the_text_names_the_target():
    known = {1: 3.5, 2: 29.0, 3: 54.5, 4: 80.0}
    nxt = rep.plan_next_pass(known, {2: "split"}, [], {}, {2: "now split by bar 1"})
    assert abs(nxt["shift_mm"] * 2 - round(nxt["shift_mm"] * 2)) < 1e-6, nxt      # the 0.5 mm grid, not quarters
    assert "image 2 (now split by bar 1) whole into an aperture" in nxt["text"], nxt["text"]
    assert nxt["targets"][0]["aperture"] == 2 and "end_on" not in nxt["targets"][0]
    print("test_planner_shift_is_exact_and_the_text_names_the_target OK")


def test_free_end_rescue_names_the_plastic_the_end_rests_on():
    # end wall at aperture 1: image whole at 10 mm, the strip's end 3 mm before it
    nxt = rep.plan_next_pass({1: 10.0}, {1: "free_end"}, [7.0], {1: 7.0}, {})
    assert nxt is not None and nxt["targets"][0]["end_on"] == "end wall at aperture 1", nxt
    assert nxt["direction"] == "towards aperture 1" and "just under the end wall at aperture 1" in nxt["text"]
    e = 7.0 + nxt["shift_mm"]
    assert -2.1 <= e <= 0.0, e
    # a bar: image in aperture 3 with the end 3 mm beyond it
    p4 = 2 * STRIP.pitch_mm + 8.0
    end = p4 + L + 3.0
    nxt = rep.plan_next_pass({4: p4}, {4: "free_end"}, [end], {4: end}, {})
    if nxt is not None:
        eo = nxt["targets"][0]["end_on"]
        assert eo.startswith("bar ") or eo.startswith("end wall at aperture "), eo
        if eo.startswith("bar "):
            assert f"just under {eo}" in nxt["text"], nxt["text"]
    print("test_free_end_rescue_names_the_plastic_the_end_rests_on OK")


def test_copy_ranking_and_report_rows_for_near_whole():
    whole_sup = {"whole": True, "free_end_near": False}
    near_sup = {"whole": False, "cut_mm": 0.1, "free_end_near": False}
    whole_free = {"whole": True, "free_end_near": True}
    near_free = {"whole": False, "cut_mm": 0.2, "free_end_near": True}
    split = {"whole": False, "split": "leading", "free_end_near": False}
    ranks = [rep.copy_rank(x) for x in (whole_sup, near_sup, whole_free, near_free, split)]
    assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == 5, ranks
    images = [
        {"number": 1, "aperture": 1, "whole": False, "split": "leading", "cut_mm": 0.1,
         "hidden_mm": 0.1, "under": "end wall at aperture 1", "free_end_near": False,
         "position_mm": -0.1, "main": "x", "located_by": "holes"},
        {"number": 2, "aperture": 1, "whole": False, "split": "trailing", "free_end_near": False,
         "hidden_mm": 7.9, "under": "bar 1", "position_mm": 25.2, "main": None,
         "located_by": "holes+frame", "end_gap_mm": 3.3},
    ]
    r = rep.build_pass_report(images, [])
    assert r[0]["state"] == "near_whole" and r[0]["where"] == {"kind": "aperture", "index": 1}
    assert r[0]["cut_mm"] == 0.1 and r[0]["hidden_mm"] == 0.1 and r[0]["under"] == "end wall at aperture 1"
    assert r[0]["outcome"] == "new"
    assert r[1]["state"] == "split" and r[1]["hidden_mm"] == 7.9 and r[1]["under"] == "bar 1"
    assert r[1]["end_gap_mm"] == 3.3 and r[1]["outcome"] == "not_delivered"
    lines = rep.format_pass_report(r)
    assert lines[0] == ("  aperture 1: image 1 near-whole, 0.1 mm under the end wall at aperture 1 "
                        "(delivered, strip end supported)"), lines
    assert "bar 1: image 2 split, 7.9 mm under bar 1, the strip's end is 3.3 mm from it" in lines[1], lines
    print("test_copy_ranking_and_report_rows_for_near_whole OK")


def test_free_end_rescue_uses_the_measured_gap_and_hides_the_end_just_under_the_plastic():
    # Test 106 pass 2: film start 13.7 mm, image 1 at 16.99 mm (end gap 3.3 mm).
    nxt = rep.plan_next_pass({1: 16.99}, {1: "free_end"}, [13.7], {1: 13.7}, {1: "now whole, with the strip end free"})
    t = nxt["targets"][0]
    assert nxt["direction"] == "towards aperture 1" and abs(nxt["shift_mm"] + 14.7) <= 1.0, nxt
    assert t["end_on"] == "end wall at aperture 1" and 1.5 <= t["border_mm"] <= 3.0, t
    assert "just under the end wall at aperture 1" in nxt["text"] and "do not push" in nxt["text"], nxt["text"]
    # an end too close to the image (0.3 mm) cannot rest on plastic with the image whole
    assert rep.plan_next_pass({1: 16.99}, {1: "free_end"}, [16.69], {1: 16.69}, {}) is None
    print("test_free_end_rescue_uses_the_measured_gap_and_hides_the_end_just_under_the_plastic OK")


def main() -> int:
    tests = [
        test_legend_is_one_line_built_from_the_one_constant,
        test_holder_geometry_words,
        test_amounts_are_quarter_images_with_mm_in_brackets,
        test_pass_report_is_ordered_along_the_holder_and_carries_outcomes,
        test_strip_bounds_front_and_rear,
        test_planner_slides_a_split_image_into_an_aperture,
        test_planner_never_pushes_the_strip_out_of_the_holder,
        test_planner_rescues_a_free_end_image_only_with_the_end_on_plastic,
        test_planner_prefers_the_smaller_move_when_margins_are_comfortable,
        test_planner_shift_is_exact_and_the_text_names_the_target,
        test_free_end_rescue_names_the_plastic_the_end_rests_on,
        test_copy_ranking_and_report_rows_for_near_whole,
        test_free_end_rescue_uses_the_measured_gap_and_hides_the_end_just_under_the_plastic,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
