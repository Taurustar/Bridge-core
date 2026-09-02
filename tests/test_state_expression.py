"""State expression tests (plan section 15.7)."""

from __future__ import annotations

import unittest
from pathlib import Path

from core.state_expression import available_sections, build_state_block

REPO_ROOT = Path(__file__).resolve().parent.parent

SAMPLE_STATE_MD = """\
# STATE EXPRESSION

## energy:fine

## energy:low

## energy:critical

## stress:fine

## social_battery:low

## bond:secure

## bond:strained

## bond:deprived
"""

SAMPLE_STATE_MD = """\
# STATE EXPRESSION

## energy:fine

## energy:low

## energy:critical

## stress:fine

## social_battery:low

## bond:secure

## bond:strained

## bond:deprived
"""


class StateExpressionTest(unittest.TestCase):
    def test_available_sections_parsed(self):
        known = available_sections(SAMPLE_STATE_MD)
        self.assertIn(("energy", "low"), known)
        self.assertIn(("bond", "deprived"), known)
        self.assertNotIn(("hunger", "critical"), known)

    def test_block_lists_only_authored_zones(self):
        block = build_state_block(
            {"energy": "low", "stress": "fine", "hunger": "critical",
             "social_battery": "low", "bond": "strained"},
            SAMPLE_STATE_MD,
        )
        self.assertIn("[CHARACTER STATE]", block)
        self.assertIn("energy: low", block)
        self.assertIn("stress: fine", block)
        self.assertIn("social battery: low", block)
        self.assertIn("bond: strained", block)
        # hunger:critical is not authored in the sample file -> skipped.
        self.assertNotIn("hunger", block)
        self.assertIn("[AGENCY THIS TURN]", block)
        self.assertIn("Do not mention numeric values", block)

    def test_no_numeric_values_ever(self):
        text = (REPO_ROOT / "identity" / "STATE.md").read_text()
        block = build_state_block(
            {name: "fine" for name in
             ("energy", "hunger", "stress", "social_battery", "fun", "hurt")},
            text,
        )
        for line in block.splitlines():
            for token in line.split(": ", 1)[-1].split():
                self.assertNotRegex(token, r"^\d+$")

    def test_no_dialogue_is_scripted(self):
        # The block contains zone lines and agency instructions only —
        # never quoted character speech.
        block = build_state_block({"energy": "low"}, SAMPLE_STATE_MD)
        self.assertNotIn('"', block)

    def test_empty_when_no_zones_apply(self):
        self.assertEqual(build_state_block({"energy": "fine"}, ""), "")
        self.assertEqual(build_state_block({}, SAMPLE_STATE_MD), "")

    def test_shipped_state_template_sections_parse(self):
        text = (REPO_ROOT / "identity" / "STATE.md").read_text()
        known = available_sections(text)
        self.assertIn(("energy", "fine"), known)
        self.assertIn(("bond", "deprived"), known)


if __name__ == "__main__":
    unittest.main()
