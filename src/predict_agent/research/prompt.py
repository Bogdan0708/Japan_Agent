"""The forecasting prompt (spec §3 "Forecasting without the price", §5 "About the range").

The prompt is frozen as one artifact (system text, user template and output schema) and
its hash is part of the cohort identity, so any wording or schema change opens a new
cohort. A rendered prompt contains only the question, the pinned rules version, the end
date and today's date: never a price, order book, volume or any market statistic."""

from __future__ import annotations

from datetime import datetime

from ..util import canonical_json, isoformat
from .schema import OUTPUT_SCHEMA

SYSTEM_PROMPT = """You are a careful forecaster. You estimate the probability that a \
question resolves YES under its written rules, using only evidence you find on the open \
web with the WebSearch and WebFetch tools.

Rules for your research:
- Never look up prediction markets, betting exchanges, bookmakers or odds aggregators, \
and never use market prices, odds or crowd forecasts as evidence. Your estimate must be \
your own.
- Read the resolution rules literally. If they are ambiguous enough that a careful reader \
could not say what resolves YES, abstain and say why.
- Every evidence item must cite a URL you actually opened with WebFetch in this session.
- Give a base rate: how often comparable situations resolved YES, from reference-class \
evidence, before considering the specifics.

Report three probabilities as decimal strings between "0.01" and "0.99": p_low and \
p_high are the lowest and highest probability you would still find defensible given the \
evidence, and p_mid is your best estimate, with p_low <= p_mid <= p_high."""

USER_TEMPLATE = """Question: {question}

Resolution rules (verbatim):
{rules_text}

Resolution source: {resolution_source}
Market end date (UTC): {end_date}
Today (UTC): {today}

Research the question, then return your forecast in the required structured format."""


def prompt_artifact() -> str:
    """Canonical content of the prompt artifact frozen into the cohort identity."""
    return canonical_json(
        {"system": SYSTEM_PROMPT, "user_template": USER_TEMPLATE, "output_schema": OUTPUT_SCHEMA}
    )


def render_user_prompt(
    *,
    question: str,
    rules_text: str,
    resolution_source: str,
    end_date: datetime,
    today: datetime,
) -> str:
    return USER_TEMPLATE.format(
        question=question,
        rules_text=rules_text,
        resolution_source=resolution_source or "(not stated)",
        end_date=isoformat(end_date),
        today=isoformat(today)[:10],
    )


def research_input(user_prompt: str) -> str:
    """Canonical content of the research_input artifact: exactly what was sent."""
    return canonical_json({"system": SYSTEM_PROMPT, "user": user_prompt})
