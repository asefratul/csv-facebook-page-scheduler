"""Minimal Claude API client (plain HTTPS, no SDK). Every call is a tool call (the prompt demands exactly one), so
the model can only return JSON matching a schema we supply. It has no tools, no repo access and no way to act:
callers validate the JSON and the code does any writing.
"""
import os
import time

import requests

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"


class LLMError(RuntimeError):
    pass


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _model():
    if os.environ.get("CLAUDE_MODEL"):
        return os.environ["CLAUDE_MODEL"]
    from . import config
    try:
        return config.load()["model"] or DEFAULT_MODEL
    except ValueError:
        return DEFAULT_MODEL


def _reason(r):
    """The API's own error type/message (it describes our request, never our key) to make 4xx errors debuggable."""
    try:
        e = r.json().get("error", {})
        return f" ({e.get('type', '?')}: {str(e.get('message', ''))[:300]})"
    except Exception:
        return ""


def call_json(system, user, schema, tool_name="submit", max_tokens=4000, model=None,
              post=requests.post, sleep=time.sleep):
    """Ask the model for one JSON object matching `schema` (a JSON-schema dict). Returns the parsed dict."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    body = {
        "model": model or _model(),
        "max_tokens": max_tokens,
        "system": system + f"\n\nAnswer by calling the `{tool_name}` tool exactly once, with nothing else.",
        "messages": [{"role": "user", "content": user}],
        "tools": [{"name": tool_name, "description": "Submit the result.", "input_schema": schema}],
        "tool_choice": {"type": "auto"},  # this model rejects forced tool_choice; the prompt demands the call
    }
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    last = None
    for attempt in range(4):
        try:
            r = post(API_URL, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:  # network hiccup; the message is not logged (could echo headers)
            last = f"network error: {type(e).__name__}"
        else:
            if r.status_code == 200:
                for block in r.json().get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == tool_name:
                        return block["input"]
                raise LLMError("model returned no tool result")
            last = f"HTTP {r.status_code}"
            if r.status_code not in (408, 429, 500, 502, 503, 529):
                raise LLMError(f"Claude API error: {last}{_reason(r)}")
        sleep(2 ** attempt * 3)
    raise LLMError(f"Claude API failed after retries ({last})")
