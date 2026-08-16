"""Shared test doubles."""


class ScriptedClient:
    """Returns queued assistant messages in order, recording what it was sent."""

    def __init__(self, *messages):
        self.queue = list(messages)
        self.seen = []

    async def chat(self, messages, **kw):
        # Snapshot: the harness keeps appending to this list after the call.
        self.seen.append((list(messages), kw))
        return self.queue.pop(0) if self.queue else {"role": "assistant", "content": "done"}


def reply(content=None, tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def call(name, arguments="{}", id="c1"):
    return {"id": id, "type": "function",
            "function": {"name": name, "arguments": arguments}}
