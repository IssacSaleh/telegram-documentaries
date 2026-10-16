"""Shared ADK utilities.

Extracted from app.bouncer to be reused by the Interviewer while keeping
behaviour identical.
"""

from __future__ import annotations


def extract_json(text: str) -> str:
    """Pull the FIRST complete JSON object out of a model response.

    Structured output is the contract, but a model occasionally narrates
    ("Here is my verdict: {...}") before answering, or mentions braces in
    prose. Tolerating that is cheaper than a failed classification, and the
    object is still validated by Pydantic models on the way in.

    A brace-balancer (string-aware, so braces inside values don't confuse it)
    stops us at the first closed object instead of greedily spanning the
    whole response — which would swallow any prose after it. If nothing
    balances, the text is passed through untouched so validation fails fast
    and the caller degrades loudly rather than guessing.
    """
    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return text
