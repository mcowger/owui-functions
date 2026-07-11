from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from owui_manifolds.shared.history_store import OpenWebUIItemStore
from owui_manifolds.shared.rendering import format_tool_call_details, parse_tool_call_attrs
from owui_manifolds.shared.tools import SharedOpenWebUIToolExecutor, SharedToolCall


DIRECT_TOOL_ENTRY = {
    "spec": {
        "name": "list_files",
        "description": "Return a structured listing of files and directories.",
        "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}},
    },
    "direct": True,
    "server": {"url": "http://127.0.0.1:8889"},
}


def test_build_check_is_clean():
    result = subprocess.run(
        [sys.executable, "scripts/build_functions.py", "--check"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_dist_bundles_import_and_expose_pipe():
    for filename in ("responses.py", "gemini.py", "anthropic_function.py"):
        path = REPO_ROOT / "dist" / filename
        spec = importlib.util.spec_from_file_location(f"bundle_{filename[:-3]}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        pipe = module.Pipe()
        assert pipe is not None

        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
        metadata = {}
        prepared = asyncio.run(
            module.prepare_context_for_pipe(
                body,
                model_id="test",
                user=None,
                chat_id=None,
                metadata=metadata,
            )
        )
        assert prepared is body
        assert module.context_is_prepared(body, metadata)


def _install_context_open_webui_stubs(monkeypatch):
    open_webui = types.ModuleType("open_webui")
    open_webui_models = types.ModuleType("open_webui.models")
    open_webui_chats = types.ModuleType("open_webui.models.chats")
    open_webui_internal = types.ModuleType("open_webui.internal")
    open_webui_db = types.ModuleType("open_webui.internal.db")

    open_webui.__path__ = []
    open_webui_models.__path__ = []
    open_webui_internal.__path__ = []

    class Chat:
        pass

    class Chats:
        @staticmethod
        async def get_chat_by_id(chat_id):
            return None

    def get_async_db_context():
        raise AssertionError("database context should not be used during import")

    open_webui_chats.Chat = Chat
    open_webui_chats.Chats = Chats
    open_webui_db.get_async_db_context = get_async_db_context
    open_webui.models = open_webui_models
    open_webui_models.chats = open_webui_chats
    open_webui.internal = open_webui_internal
    open_webui_internal.db = open_webui_db

    monkeypatch.setitem(sys.modules, "open_webui", open_webui)
    monkeypatch.setitem(sys.modules, "open_webui.models", open_webui_models)
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", open_webui_chats)
    monkeypatch.setitem(sys.modules, "open_webui.internal", open_webui_internal)
    monkeypatch.setitem(sys.modules, "open_webui.internal.db", open_webui_db)


def test_dist_context_bundle_imports_and_exposes_filter(monkeypatch):
    _install_context_open_webui_stubs(monkeypatch)

    path = REPO_ROOT / "dist" / "context.py"
    spec = importlib.util.spec_from_file_location("bundle_context", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.Filter.Valves.model_rebuild()
    module.Filter.UserValves.model_rebuild()

    filter_obj = module.Filter()
    assert filter_obj is not None


def test_responses_strict_schema_adds_type_to_constrained_anyof_branch():
    path = SRC_ROOT / "owui_manifolds/providers/responses/pipe.py"
    spec = importlib.util.spec_from_file_location("responses_schema_regression", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {
                "anyOf": [{"minimum": 1}, {"type": "null"}],
                "default": None,
            },
        },
        "required": ["path"],
    }

    strict = module._strictify_schema(schema)
    start_line = strict["properties"]["start_line"]
    assert start_line["anyOf"][0]["type"] == "integer"
    assert start_line["anyOf"][0]["minimum"] == 1
    assert start_line["anyOf"].count({"type": "null"}) == 1
    assert "start_line" in strict["required"]
    assert strict["additionalProperties"] is False


def test_responses_registry_schema_repaired_when_strict_disabled():
    path = SRC_ROOT / "owui_manifolds/providers/responses/pipe.py"
    spec = importlib.util.spec_from_file_location("responses_registry_schema_regression", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    cfg = module.PipeValves(ENABLE_STRICT_TOOL_CALLING=False)
    registry = {
        "read_file": {
            "direct": True,
            "server": {"url": "http://terminal.local"},
            "spec": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {
                            "anyOf": [{"minimum": 1}, {"type": "null"}],
                            "default": None,
                        },
                    },
                    "required": ["path"],
                },
            },
        }
    }

    tools = module.ToolPolicy.build_responses_tools(
        model_id="gpt-5.5",
        cfg=cfg,
        registry=module.OpenWebUIToolRegistry(registry),
        body_tools=[],
        extra_tools=[],
        mcp_tools=[],
        web_search_tools=[],
    )

    read_file = tools[0]
    start_line = read_file["parameters"]["properties"]["start_line"]
    assert read_file.get("strict") is not True
    assert start_line["anyOf"][0]["type"] == "integer"
    assert start_line["anyOf"][0]["minimum"] == 1


def test_responses_prepare_payload_repairs_final_tool_shapes():
    path = SRC_ROOT / "owui_manifolds/providers/responses/pipe.py"
    spec = importlib.util.spec_from_file_location("responses_prepare_payload_schema_regression", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    bad_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"anyOf": [{"minimum": 1}, {"type": "null"}]},
        },
    }
    payload = module.prepare_payload(
        {
            "model": "gpt-5.5",
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": bad_parameters,
                },
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": bad_parameters,
                    },
                },
            ],
        }
    )

    top_level = payload["tools"][0]["parameters"]["properties"]["start_line"]
    nested = payload["tools"][1]["function"]["parameters"]["properties"]["start_line"]
    assert top_level["anyOf"][0]["type"] == "integer"
    assert nested["anyOf"][0]["type"] == "integer"


def test_responses_prepare_payload_repairs_real_badreq_fixture():
    path = SRC_ROOT / "owui_manifolds/providers/responses/pipe.py"
    spec = importlib.util.spec_from_file_location("responses_badreq_fixture_regression", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/responses_badreq_open_terminal.json").read_text(
            encoding="utf-8"
        )
    )
    payload = module.prepare_payload(fixture)
    read_file = next(tool for tool in payload["tools"] if tool.get("name") == "read_file")

    start_line = read_file["parameters"]["properties"]["start_line"]
    end_line = read_file["parameters"]["properties"]["end_line"]
    assert start_line["anyOf"][0]["type"] == "integer"
    assert end_line["anyOf"][0]["type"] == "integer"

    by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
    assert by_name["grep_search"]["parameters"]["properties"]["include"]["anyOf"][0]["type"] == "string"
    assert by_name["glob_search"]["parameters"]["properties"]["exclude"]["anyOf"][0]["type"] == "string"
    assert by_name["glob_search"]["parameters"]["properties"]["type"]["anyOf"][0]["type"] == "string"
    assert by_name["run_command"]["parameters"]["properties"]["wait"]["anyOf"][0]["type"] == "number"
    assert by_name["run_command"]["parameters"]["properties"]["tail"]["anyOf"][0]["type"] == "integer"
    assert by_name["get_process_status"]["parameters"]["properties"]["wait"]["anyOf"][0]["type"] == "number"
    assert by_name["get_process_status"]["parameters"]["properties"]["tail"]["anyOf"][0]["type"] == "integer"

    def assert_no_untyped_anyof_branch(value, path="root"):
        if isinstance(value, dict):
            variants = value.get("anyOf")
            if isinstance(variants, list):
                for index, variant in enumerate(variants):
                    if isinstance(variant, dict):
                        assert "type" in variant, f"{path}.anyOf[{index}] missing type: {variant!r}"
            for key, child in value.items():
                assert_no_untyped_anyof_branch(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                assert_no_untyped_anyof_branch(child, f"{path}[{index}]")

    assert_no_untyped_anyof_branch(payload["tools"])


def test_shared_direct_tool_dispatches_via_event_call():
    captured: list[dict] = []

    async def fake_event_call(event: dict):
        captured.append(event)
        return {"entries": ["a.txt"]}

    async def run():
        executor = SharedOpenWebUIToolExecutor(
            {"list_files": DIRECT_TOOL_ENTRY},
            event_call=fake_event_call,
            metadata={"session_id": "sess-1"},
        )
        return await executor.execute(
            [SharedToolCall(call_id="call_1", name="default_api:list_files", arguments={"directory": "/"})]
        )

    results = asyncio.run(run())
    assert len(captured) == 1
    assert captured[0]["type"] == "execute:tool"
    assert captured[0]["data"]["name"] == "list_files"
    assert captured[0]["data"]["params"] == {"directory": "/"}
    assert captured[0]["data"]["session_id"] == "sess-1"
    assert results[0].status == "ok"
    assert "entries" in results[0].output_text


def test_shared_direct_tool_without_event_call_is_clear_error():
    async def run():
        executor = SharedOpenWebUIToolExecutor({"list_files": DIRECT_TOOL_ENTRY}, event_call=None)
        return await executor.execute([SharedToolCall(call_id="call_1", name="list_files", arguments={})])

    result = asyncio.run(run())[0]
    assert result.status == "error"
    assert "requires __event_call__" in result.output_text


def test_shared_callable_duplicate_names_are_serialized():
    active = 0
    max_active = 0

    async def fake_tool(value: int):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"value": value}

    async def run():
        executor = SharedOpenWebUIToolExecutor(
            {
                "same_tool": {
                    "callable": fake_tool,
                    "spec": {"name": "same_tool", "parameters": {"type": "object"}},
                }
            },
            parallel=True,
        )
        return await executor.execute(
            [
                SharedToolCall(call_id="a", name="same_tool", arguments={"value": 1}),
                SharedToolCall(call_id="b", name="same_tool", arguments={"value": 2}),
            ]
        )

    results = asyncio.run(run())
    assert [result.status for result in results] == ["ok", "ok"]
    assert max_active == 1


def test_tool_details_rendering_truncates_and_parses_attrs():
    rendered = format_tool_call_details(
        tool_id="toolu_1",
        name="search",
        args={"query": "abc"},
        output="x" * 100,
        max_chars=10,
        ref="01ABCDEFGHJKLMN",
    )
    attrs = parse_tool_call_attrs(rendered.split("<details type=\"tool_calls\"", 1)[1])
    assert attrs["id"] == "toolu_1"
    assert attrs["name"] == "search"
    assert attrs["ref"] == "01ABCDEFGHJKLMN"
    assert "truncated" in attrs["result"]
    assert "x" * 100 not in rendered


class _FakeChatModel:
    def __init__(self, chat: dict):
        self.chat = chat


class _AsyncChats:
    def __init__(self):
        self._chats: dict[str, dict] = {}

    async def get_chat_by_id(self, chat_id: str):
        return _FakeChatModel(self._chats.setdefault(chat_id, {}))

    async def update_chat_by_id(self, chat_id: str, chat: dict):
        self._chats[chat_id] = chat


class _SyncChats(_AsyncChats):
    def get_chat_by_id(self, chat_id: str):
        return _FakeChatModel(self._chats.setdefault(chat_id, {}))

    def update_chat_by_id(self, chat_id: str, chat: dict):
        self._chats[chat_id] = chat


def test_item_store_supports_async_and_sync_chats():
    async def run(chats):
        store = OpenWebUIItemStore(root_key="test_pipe", chats_model=chats)
        item_id = await store.save_item("chat-1", {"output": "ok"}, model_id="model-1", message_id="msg-1")
        assert item_id
        loaded = await store.load_item("chat-1", item_id, model_id="model-1")
        return loaded

    assert asyncio.run(run(_AsyncChats())) == {"output": "ok"}
    assert asyncio.run(run(_SyncChats())) == {"output": "ok"}
