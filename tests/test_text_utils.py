"""Text pipeline tests: emotion segments, scrubbers, chunking (plan 13, 12.23)."""

from __future__ import annotations

import unittest

from core.text_utils import (
    chunk_segments,
    join_segments,
    normalize_emotion,
    parse_emotion_reply,
    parse_emotion_segments,
    split_long_text,
    strip_asterisk_actions,
    strip_control_tags,
    strip_reasoning_blocks,
)


class NormalizeEmotionTest(unittest.TestCase):
    def test_known_emotions_pass_through(self):
        self.assertEqual(normalize_emotion("happy"), "happy")
        self.assertEqual(normalize_emotion("Happy"), "happy")

    def test_unknown_normalizes_to_neutral(self):
        self.assertEqual(normalize_emotion("ecstatic"), "neutral")
        self.assertEqual(normalize_emotion(""), "neutral")
        self.assertEqual(normalize_emotion(None), "neutral")


class ParseSegmentsTest(unittest.TestCase):
    def test_single_tag(self):
        segments = parse_emotion_segments("[EMOTION: happy]\nHi there.")
        self.assertEqual(segments, [{"text": "Hi there.", "emotion": "happy"}])

    def test_multi_segment_per_segment_emotions(self):
        raw = "[EMOTION: happy]\nFirst.\n[EMOTION: serious]\nSecond."
        self.assertEqual(
            parse_emotion_segments(raw),
            [
                {"text": "First.", "emotion": "happy"},
                {"text": "Second.", "emotion": "serious"},
            ],
        )

    def test_unknown_segment_emotion_becomes_neutral(self):
        segments = parse_emotion_segments("[EMOTION: ecstatic]\nWheee.")
        self.assertEqual(segments[0]["emotion"], "neutral")

    def test_leading_text_becomes_neutral_segment(self):
        segments = parse_emotion_segments("Oh hi.\n[EMOTION: happy]\nThere.")
        self.assertEqual(
            segments,
            [
                {"text": "Oh hi.", "emotion": "neutral"},
                {"text": "There.", "emotion": "happy"},
            ],
        )

    def test_no_tags_single_neutral_segment(self):
        self.assertEqual(
            parse_emotion_segments("Just talking."),
            [{"text": "Just talking.", "emotion": "neutral"}],
        )

    def test_emotion_only_yields_no_segments(self):
        self.assertEqual(parse_emotion_segments("[EMOTION: happy]"), [])
        self.assertEqual(parse_emotion_segments(""), [])

    def test_blank_segments_dropped(self):
        raw = "[EMOTION: happy]\n\n[EMOTION: serious]\nReal text."
        self.assertEqual(
            parse_emotion_segments(raw),
            [{"text": "Real text.", "emotion": "serious"}],
        )


class ScrubTest(unittest.TestCase):
    def test_reasoning_block_paired_stripped(self):
        raw = "<think>hidden reasoning</think>[EMOTION: happy]\nVisible."
        self.assertEqual(
            parse_emotion_segments(raw), [{"text": "Visible.", "emotion": "happy"}]
        )

    def test_reasoning_block_unclosed_at_start_swallows_rest(self):
        # Nothing displayable follows an unclosed leading reasoning block;
        # the reply parses empty and is retried per plan 7.3.
        raw = "<think>internal...[EMOTION: happy]\nVisible."
        self.assertEqual(parse_emotion_segments(raw), [])

    def test_reasoning_variant_tags(self):
        for tag in ("think", "thinking", "reasoning"):
            cleaned = strip_reasoning_blocks(f"<{tag}>x</{tag}>y")
            self.assertEqual(cleaned, "y")

    def test_control_tags_stripped(self):
        self.assertEqual(
            strip_control_tags("A [STATUS: question] B [TOOL_CALL: x] C"),
            "A  B  C",
        )

    def test_lowercase_bracket_prose_survives(self):
        self.assertEqual(
            strip_control_tags("See [Note: this is fine] ok"), "See [Note: this is fine] ok"
        )

    def test_status_tag_inside_reply_segment(self):
        raw = "[EMOTION: happy]\nSure![STATUS: question] Anything else?"
        self.assertEqual(
            parse_emotion_segments(raw),
            [{"text": "Sure! Anything else?", "emotion": "happy"}],
        )

    def test_asterisk_actions_stripped(self):
        # The scrubber removes the action span; surrounding trimming belongs
        # to the segment parser, not the raw scrubber.
        self.assertEqual(strip_asterisk_actions("*waves* Hi"), " Hi")


class CompatTest(unittest.TestCase):
    def test_parse_emotion_reply_joins_segments(self):
        raw = "[EMOTION: happy]\nFirst.\n[EMOTION: serious]\nSecond."
        text, emotion = parse_emotion_reply(raw)
        self.assertEqual(text, "First.\n\nSecond.")
        self.assertEqual(emotion, "happy")

    def test_join_segments(self):
        self.assertEqual(
            join_segments([{"text": "a"}, {"text": "b"}]), "a\n\nb"
        )


class SplitLongTextTest(unittest.TestCase):
    def test_packs_sentences_up_to_size(self):
        text = "One two. Three four. Five six."
        pieces = split_long_text(text, 20)
        self.assertEqual(pieces, ["One two. Three four.", "Five six."])

    def test_each_sentence_own_piece_when_size_tight(self):
        text = "One two. Three four. Five six."
        pieces = split_long_text(text, 12)
        self.assertEqual(pieces, ["One two.", "Three four.", "Five six."])

    def test_oversized_single_sentence_kept_whole(self):
        text = "A single very long sentence without internal breaks"
        self.assertEqual(split_long_text(text, 10), [text])

    def test_cjk_sentence_marks(self):
        text = "こんにちは。 元気です。 ありがとう。"
        pieces = split_long_text(text, 6)
        self.assertEqual(pieces, ["こんにちは。", "元気です。", "ありがとう。"])


class ChunkSegmentsTest(unittest.TestCase):
    def test_short_reply_not_split(self):
        segments = [
            {"text": "Short one.", "emotion": "happy"},
            {"text": "Short two.", "emotion": "serious"},
        ]
        self.assertEqual(chunk_segments(segments, threshold=150, size=150), segments)

    def test_packs_consecutive_same_emotion_up_to_size(self):
        segments = [
            {"text": "aaa.", "emotion": "happy"},
            {"text": "bbb.", "emotion": "happy"},
            {"text": "ccc.", "emotion": "happy"},
        ]
        chunks = chunk_segments(segments, threshold=5, size=9)
        self.assertEqual(
            chunks,
            [
                {"text": "aaa. bbb.", "emotion": "happy"},
                {"text": "ccc.", "emotion": "happy"},
            ],
        )

    def test_never_packs_across_emotion_change(self):
        segments = [
            {"text": "aaa.", "emotion": "happy"},
            {"text": "bbb.", "emotion": "serious"},
        ]
        chunks = chunk_segments(segments, threshold=5, size=150)
        self.assertEqual(chunks, segments)

    def test_oversized_segment_splits_keeping_emotion(self):
        segments = [{"text": "One two. Three four. Five.", "emotion": "worried"}]
        chunks = chunk_segments(segments, threshold=5, size=12)
        self.assertEqual(
            chunks,
            [
                {"text": "One two.", "emotion": "worried"},
                {"text": "Three four.", "emotion": "worried"},
                {"text": "Five.", "emotion": "worried"},
            ],
        )

    def test_empty_segments_never_chunked(self):
        self.assertEqual(
            chunk_segments([{"text": "  ", "emotion": "happy"}], 150, 150), []
        )

    def test_threshold_zero_always_packs(self):
        segments = [{"text": "aaa.", "emotion": "happy"}, {"text": "bbb.", "emotion": "happy"}]
        self.assertEqual(
            chunk_segments(segments, threshold=0, size=150),
            [{"text": "aaa. bbb.", "emotion": "happy"}],
        )


if __name__ == "__main__":
    unittest.main()
