#!/usr/bin/env python3
"""PostToolUse hook（matcher: Read）：记录记忆库文件被读取的时间。

只关心落在 ~/.memory 下的文件，其他路径立刻退出，开销极小。
不往 stdout 写任何东西——PostToolUse 的输出会进上下文，没必要占位置。

为什么需要它：SessionStart 只注入 7 个常驻文件，分册里的子文件是按需
读取的，走不到注入脚本。没有这个钩子，子文件的访问时间永远是空的，
归档判断就只能靠猜。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _state import (
    find_root,
    log,
    relative_to_root,
    resolve_agent,
    session_agent,
    touch_access,
)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except Exception as exc:
        log(f"输入解析失败：{exc}")
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    # 不同工具/版本可能用不同的键名，挨个试
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("filePath")
        or ""
    )

    root = find_root()
    rel = relative_to_root(root, file_path)
    if not rel:
        return 0

    # 这个 hook 的 payload 没有 transcript_path，判不出 harness，
    # 只能靠 SessionStart 留下的「会话 → agent」映射查回来。
    agent = session_agent(root, str(payload.get("session_id", "")))
    if not agent:
        agent, _ = resolve_agent(root, payload)

    touch_access(root, [rel], agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
