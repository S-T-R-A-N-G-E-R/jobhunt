import httpx

API_URL = "https://api.anthropic.com/v1/messages"


def complete(http: httpx.Client, api_key: str, model: str, prompt: str, schema: dict) -> dict:
    """Forced tool use so the output follows the schema. Returns the tool input dict."""
    r = http.post(
        API_URL,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "max_tokens": 1024,
            "temperature": 0,
            "tools": [
                {"name": "submit", "description": "Submit the result.", "input_schema": schema}
            ],
            "tool_choice": {"type": "tool", "name": "submit"},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    for block in r.json()["content"]:
        if block["type"] == "tool_use":
            return block["input"]
    raise ValueError("no tool_use block in Anthropic response")
