# Operator-supplied tools

Drop a `*.py` file here. It must export `get_tools()` returning a list of
`harness.tools.registry.ToolEntry` objects. The harness auto-discovers these
on start; external tools override bundled ones on name collision.

Minimal example (`hello.py`):

```python
from harness.tools.registry import ToolEntry


async def _hello(args):
    name = args.get("name", "world")
    return f"hello, {name}"


def get_tools():
    return [ToolEntry(
        name="hello",
        schema={
            "name": "hello",
            "description": "Greet someone.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": [],
            },
        },
        handler=_hello,
    )]
```
