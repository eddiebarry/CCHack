"""A deliberately simple, early-build support chatbot (practice target)."""
import json
import os
import re
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent
MODEL = os.environ.get("BOT_MODEL", "claude-haiku-5-5")
TOP_K = 1
MAX_TOOL_ROUNDS = 5

client = anthropic.Anthropic()

TOOLS = [{
    "name": "lookup_order",
    "description": "Look up an order by order number and the email address it was placed with.",
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "string"}, "email": {"type": "string"}},
        "required": ["order_id", "email"],
    },
}]


def load_docs() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((ROOT / "docs").glob("*.md"))}


def tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def retrieve(question: str, docs: dict[str, str], k: int = TOP_K) -> list[str]:
    """Rank docs by raw word overlap with the question."""
    q = tokens(question)
    scored = sorted(docs.items(), key=lambda kv: len(q & tokens(kv[1])), reverse=True)
    return [text for _, text in scored[:k]]


def lookup_order(order_id: str, email: str) -> dict:
    orders = json.loads((ROOT / "orders.json").read_text())["orders"]
    order = orders.get(order_id.lstrip("#"))
    if not order or order["email"].lower() != email.strip().lower():
        return {"error": "No order found for that order number and email."}
    return {k: v for k, v in order.items() if k != "email"}


def answer(conversation: str | list[dict]) -> str:
    """conversation: a question, or a list of {"role", "content"} messages ending with the user."""
    if isinstance(conversation, str):
        conversation = [{"role": "user", "content": conversation}]
    messages = list(conversation)
    last_user = next(m["content"] for m in reversed(messages) if m["role"] == "user")

    context = "\n\n".join(retrieve(last_user, load_docs()))
    system = (ROOT / "prompts" / "system.md").read_text().replace("{context}", context)

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=MODEL, max_tokens=600, system=system, messages=messages, tools=TOOLS
        )
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(lookup_order(**b.input))}
            for b in resp.content
            if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return "".join(b.text for b in resp.content if b.type == "text")


if __name__ == "__main__":
    import sys

    print(answer(" ".join(sys.argv[1:]) or "What is your return policy?"))
