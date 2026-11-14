"""Matcher (interpretation stage A): recording -> candidates.json. Pure, no network, no LLM."""

from matcher.core import char_class, label_regex, left_label, match

__all__ = ["match", "char_class", "label_regex", "left_label"]
