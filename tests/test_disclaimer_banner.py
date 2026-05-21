# SPDX-License-Identifier: Apache-2.0
"""Regression guard: every user-facing web page carries the
reference-implementation disclaimer banner.

Mirrors the JSON-LD `test_disclaimer_always_present` guard
(tests/test_claimreview.py) for the HTML surface. The two together
ensure a casual human visitor sees the same caveat that an
automated agent crawling the ClaimReview JSON-LD payload does.

Rationale: every page has a small footer that says "automated
analysis · not legal advice", but the footer is below the fold
on most viewports and easy to miss. The banner directly under
the nav is the load-bearing version of the disclaimer; this test
pins it.

This file also pins the canonical disclaimer string in
kahzaabu/claimreview.py so a future edit that softens the
"sample / reference implementation" framing fails CI loudly
instead of silently shipping a weaker disclaimer to every
ClaimReview JSON-LD blob downstream of this project.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "kahzaabu" / "web" / "static"


class DisclaimerBannerTests(unittest.TestCase):
    """The site-wide banner must appear above <main> on every page."""

    @classmethod
    def setUpClass(cls):
        cls.pages = sorted(STATIC.glob("*.html"))
        # If this assert fails, somebody renamed/moved the static
        # dir — investigate before relaxing it.
        assert cls.pages, f"no HTML pages under {STATIC}"

    def test_every_page_has_banner_div(self):
        for page in self.pages:
            text = page.read_text()
            with self.subTest(page=page.name):
                self.assertIn(
                    'class="site-disclaimer"', text,
                    f"{page.name} is missing the .site-disclaimer "
                    "banner. The banner is the load-bearing "
                    "reference-implementation caveat — removing it "
                    "from any page lets a visitor land on content "
                    "without the framing.")

    def test_every_page_calls_out_reference_implementation(self):
        for page in self.pages:
            text = page.read_text()
            with self.subTest(page=page.name):
                self.assertIn(
                    "Reference implementation", text,
                    f"{page.name}'s banner text was softened — "
                    "the 'Reference implementation' framing is what "
                    "the disclaimer is for.")

    def test_every_page_links_to_full_disclaimer(self):
        for page in self.pages:
            text = page.read_text()
            with self.subTest(page=page.name):
                self.assertIn(
                    'href="/disclaimer"', text,
                    f"{page.name} doesn't link to /disclaimer — "
                    "the banner is meant to be a teaser, not the "
                    "whole disclaimer; without the link, the full "
                    "terms are unreachable from the page.")

    def test_banner_appears_before_main(self):
        for page in self.pages:
            text = page.read_text()
            with self.subTest(page=page.name):
                banner_idx = text.find('class="site-disclaimer"')
                main_idx = text.find("<main")
                self.assertGreater(banner_idx, -1, f"{page.name}: no banner")
                self.assertGreater(main_idx, -1, f"{page.name}: no <main>")
                self.assertLess(
                    banner_idx, main_idx,
                    f"{page.name}: the banner is positioned AFTER "
                    "<main>. Move it back above the fold — the "
                    "framing must appear before the content.")

    def test_disclaimer_page_exists(self):
        page = STATIC / "disclaimer.html"
        self.assertTrue(
            page.exists(),
            "/disclaimer is referenced by every other page's banner "
            "but disclaimer.html doesn't exist — a dead link is worse "
            "than no link.")


class ClaimReviewDisclaimerWordingTests(unittest.TestCase):
    """Pin the substrings that make the ClaimReview JSON-LD
    disclaimer load-bearing. The existing test_claimreview.py
    pins 'automated analysis' and 'not legal advice'; this adds
    the 'sample / reference implementation' substrings that
    were strengthened when the project was open-sourced."""

    def test_disclaimer_says_reference_implementation(self):
        from kahzaabu.claimreview import DISCLAIMER
        self.assertIn("reference implementation", DISCLAIMER)
        self.assertIn("educational", DISCLAIMER)

    def test_disclaimer_says_not_authoritative(self):
        from kahzaabu.claimreview import DISCLAIMER
        self.assertIn("not an authoritative source", DISCLAIMER)

    def test_disclaimer_says_must_not_be_cited(self):
        from kahzaabu.claimreview import DISCLAIMER
        # Either phrasing is fine, but at least one must be present.
        self.assertTrue(
            "must not be cited" in DISCLAIMER
            or "do not cite" in DISCLAIMER.lower(),
            "The disclaimer must explicitly say the verdict is "
            "not citable. The substring 'must not be cited' or "
            "'do not cite' must appear in DISCLAIMER.")


if __name__ == "__main__":
    unittest.main()
