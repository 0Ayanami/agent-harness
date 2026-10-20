"""One representation for SDK content, persisted conversations and HTTP payloads."""

from dataclasses import asdict, is_dataclass


def json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return json_value(vars(value))
    return value


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "") for block in json_value(content) if block.get("type") == "text"
    ).strip()


def tool_calls(content) -> list[dict]:
    return [block for block in json_value(content) if block.get("type") == "tool_use"]


def repair_interrupted_turn(history: list) -> None:
    """Finish orphaned tool calls after a process restart, before calling a model."""
    pending = {}
    for message in history:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                pending[block["id"]] = block
            elif block.get("type") == "tool_result":
                pending.pop(block.get("tool_use_id"), None)
    if pending:
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "Execution interrupted before a result was recorded. Check state before retrying.",
                    }
                    for tool_id in pending
                ],
            }
        )
