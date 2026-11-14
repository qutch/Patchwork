"""The new flow (TEACH from example pages) is domain-free: no job wording in its prompts or tool descriptions."""

from __future__ import annotations

import re

PROMPTS = ["TEACH_EXAMPLES_TASK", "GENERIC_LEARNER", "TEACH_EXAMPLES_SUPERVISOR", "MAPPER", "MAPPER_DESCRIPTION",
           "EXAMPLES_LEARNER", "EXAMPLES_LEARNER_DESCRIPTION", "EXAMPLES_BUILDER"]
JOB_WORDS = re.compile(r"\b(job|posting|employer|company|hiring|applicant|resume)\b", re.I)


def test_new_flow_text_has_no_job_words():
    from agents import deep_tools, prompts
    for n in PROMPTS:
        assert not JOB_WORDS.search(getattr(prompts, n)), n
    for t in ("propose_mapping", "add_site_parser"):
        assert not JOB_WORDS.search(deep_tools.TOOL_DOCS[t])


def test_built_prompts_have_no_job_words():
    from agents import prompts
    dest = {"Product": {"type": "text", "required": True}, "Price": {"type": "number", "required": False}}
    for text in (prompts.mapper_prompt(name="x", intent="Track product prices", dest=dest,
                                       pages=[("pg-000001", "shop-a.example.com", "OUTLINE")]),
                 prompts.examples_learner_prompt(name="x", pages=[("pg-000002", "b.example.org", "OUTLINE")]),
                 prompts.teach_examples_task(name="x", intent="Track product prices", dest=dest,
                                             pages=[("pg-000001", "a.example.com"), ("pg-000002", "b.example.org")])):
        assert not JOB_WORDS.search(text), text
