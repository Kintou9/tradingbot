"""Shared helper for extracting the structured JSON block that each LLM
analyst module (dcf, deep_dive, technical_scanner) is prompted to append
after its markdown write-up.
"""

import json


def extract_json_block(text: str) -> dict:
    """Find the last balanced {...} block in text and parse it as JSON.

    Scans backward from the final '}' so it locates the *outer* object
    even when the JSON itself contains nested objects.
    """
    end = text.rfind("}")
    if end == -1:
        raise ValueError(f"No JSON block found in response: {text[:200]}")

    depth = 0
    for i in range(end, -1, -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            depth -= 1
            if depth == 0:
                return json.loads(text[i:end + 1])

    raise ValueError(f"Unbalanced JSON block in response: {text[:200]}")


def call_and_extract_json(client, model: str, prompt: str, max_tokens: int, retries: int = 2):
    """Call the model and return (raw_text, structured_dict).

    Models don't reliably comply with "end your response with this JSON
    block" every time (observed in testing — same prompt, same inputs,
    sometimes omitted). Retry with a sharper nudge rather than letting a
    single stochastic miss break the pipeline.
    """
    current_prompt = prompt
    last_error = None
    for _ in range(retries + 1):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": current_prompt}],
        )
        raw_text = next(block.text for block in response.content if block.type == "text")
        try:
            return raw_text, extract_json_block(raw_text)
        except ValueError as exc:
            last_error = exc
            current_prompt = (
                prompt
                + "\n\nYour previous response did not end with the required JSON "
                "block. Reply again, and this time end your response with EXACTLY "
                "the required JSON block and nothing after it."
            )
    raise last_error
