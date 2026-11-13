import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash-lite"


def environment_values(path):
    if not path.exists():
        return {}

    values = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")

        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            continue

        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.partition(" #")[0].rstrip()

        values[name.strip()] = value

    return values


def configuration():
    values = {**environment_values(ROOT / ".env"), **os.environ}
    api_key = values.get("API_KEY", "").strip()
    model = values.get("LLM_MODEL", "").strip() or DEFAULT_MODEL

    if not api_key:
        raise ValueError("Set API_KEY in .env or the environment")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
        raise ValueError("Invalid LLM_MODEL")

    return api_key, model


def translation_instructions():
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8").strip()

    if not instructions:
        raise ValueError("AGENTS.md is missing translation instructions")

    return instructions


def translation_schema(count):
    row = {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING", "description": "Unchanged input article ID."},
            "summary_ko": {
                "type": "STRING",
                "description": "Article summary written in Korean.",
            },
            "title_ko": {
                "type": "STRING",
                "description": "Title translated into Korean using the summary's context. Preserve only proper names in their original spelling.",
            },
        },
        "required": ["id", "summary_ko", "title_ko"],
        "propertyOrdering": ["id", "summary_ko", "title_ko"],
    }

    return {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": row,
                "minItems": count,
                "maxItems": count,
            },
        },
        "required": ["items"],
    }


def generation_config(model, count):
    settings = {
        "temperature": 0.2,
        "maxOutputTokens": min(16384, max(1024, count * 512)),
        "responseMimeType": "application/json",
        "responseSchema": translation_schema(count),
    }

    if model.startswith("gemini-3"):
        settings["thinkingConfig"] = {"thinkingLevel": "MINIMAL"}
    elif model.startswith("gemini-2.5-flash"):
        settings["thinkingConfig"] = {"thinkingBudget": 0}

    return settings


def generate_content(api_key, model, payload):
    request = Request(
        f"{API_URL}/{model}:generateContent",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )

    try:
        with urlopen(request, timeout=180) as response:
            body = response.read(2_000_001)
    except HTTPError as error:
        raise ValueError(f"LLM request failed (HTTP {error.code}); no retry") from None
    except OSError as error:
        raise ValueError(f"LLM request failed: {error}; no retry") from None

    if len(body) > 2_000_000:
        raise ValueError("LLM response exceeds size limit")

    return json.loads(body)


def translation_result(response):
    candidates = response.get("candidates", [])

    if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
        raise ValueError(
            "LLM did not complete the translations; previous feed preserved"
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    result = json.loads(text)

    if (
        not isinstance(result, dict)
        or set(result) != {"items"}
        or not isinstance(result["items"], list)
    ):
        raise ValueError("LLM must return an object containing an items array")

    rows = []

    for item in result["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "summary_ko", "title_ko"}:
            raise ValueError("Each LLM item must contain id, summary_ko, and title_ko")

        rows.append([item["id"], item["title_ko"], item["summary_ko"]])

    return {"items": rows}


def generate_translations(input_text, count):
    api_key, model = configuration()
    request_text = (
        "Input JSONL:\n"
        + input_text.rstrip()
        + "\n\n위 기사의 본문을 한국어로 요약하고, 제목도 한국어로 번역하세요."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": translation_instructions()}]},
        "contents": [{"role": "user", "parts": [{"text": request_text}]}],
        "generationConfig": generation_config(model, count),
    }
    response = generate_content(api_key, model, payload)

    return translation_result(response)
