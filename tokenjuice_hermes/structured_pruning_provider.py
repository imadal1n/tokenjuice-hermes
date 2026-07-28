"""Provider-message reconstruction for structured pruning results."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .json_types import parse_json

if TYPE_CHECKING:
    from .json_types import JsonValue
    from .structured_pruning_types import ContributionInternal


def provider_messages_from_contributions(
    retained: list[ContributionInternal],
) -> list[dict[str, JsonValue]]:
    """Reconstruct a provider-shaped message list from retained contributions."""
    system_parts: list[str] = []
    messages: list[dict[str, JsonValue]] = []
    for contribution in retained:
        if contribution.kind == "system_part":
            system_parts.append(contribution.original["content"])
            continue
        if contribution.kind == "tool_schema":
            continue
        provider_message = contribution.original.get("provider_message")
        if isinstance(provider_message, dict):
            messages.append(dict(provider_message))
            continue
        message = _provider_message_from_contribution(contribution)
        if message is not None:
            messages.append(message)

    result: list[dict[str, JsonValue]] = []
    if system_parts:
        system_content = "\n\n".join(part for part in system_parts if part)
        if system_content:
            result.append({"role": "system", "content": system_content})
    result.extend(messages)
    return result


def _provider_message_from_contribution(
    contribution: ContributionInternal,
) -> dict[str, JsonValue] | None:
    content = contribution.original["content"]
    if contribution.class_ == "user_message":
        return {"role": "user", "content": content}
    if contribution.class_ == "assistant_message":
        message: dict[str, JsonValue] = {"role": "assistant", "content": content}
        tool_calls = contribution.original.get("tool_calls")
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message
    name = _tool_name_for_class(contribution.class_)
    return {
        "role": "tool",
        "content": content,
        "name": name,
        "tool_call_id": contribution.atomic_group_id or "",
    }


def _tool_name_for_class(class_: str) -> str:
    if class_ == "terminal_tool_output":
        return "terminal"
    if class_ == "exact_file_read":
        return "read_file"
    return "tool"


def provider_tools_from_contributions(
    retained: list[ContributionInternal],
) -> list[dict[str, JsonValue]] | None:
    """Reconstruct provider-shaped tool schemas from retained tool-schema contributions."""
    tools: list[dict[str, JsonValue]] = []
    for contribution in retained:
        if contribution.kind != "tool_schema":
            continue
        provider_tool = contribution.original.get("provider_tool")
        if isinstance(provider_tool, dict):
            tools.append(dict(provider_tool))
            continue
        content = contribution.original["content"]
        try:
            parsed = parse_json(content)
        except Exception:  # noqa: BLE001, S112 - tool-schema JSON parse failures are best-effort
            continue
        if isinstance(parsed, dict):
            tools.append(parsed)
    return tools or None
