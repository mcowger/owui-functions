from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


def test_build_check_is_clean():
    result = subprocess.run(
        [sys.executable, "scripts/build_functions.py", "--check"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


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

