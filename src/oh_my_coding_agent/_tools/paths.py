from __future__ import annotations

import os


def resolve_literal_tool_path(workspace: str, path: str) -> str:
    joined = path if os.path.isabs(path) else os.path.join(workspace, path)
    return os.path.normpath(joined)
