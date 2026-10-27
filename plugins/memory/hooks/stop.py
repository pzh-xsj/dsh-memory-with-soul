#!/usr/bin/env python3
"""Stop hook（WorkBuddy）：每轮结束时问一次「该提炼了吗」。

**判定逻辑不在这里**——它在 `core/memory.py` 的 `digest_check()`。
WB 与 DSH 只是同一个判定的两个外壳：WB 把它打成 `{"continue": false,
"stopReason": ...}`，DSH 那边把它打成一串提示。

以前判定在这份文件里，WB 一旦冻结，DSH 侧就得抄一遍；抄一遍就是两个真相源，
两边必然漂移（本项目已经为「三份实现各自漂移」付过代价）。

WB 与 DSH 唯一的差异是「轮数怎么数」：WB 的 Stop payload 带
`transcript_path`，数里面的 user 消息；DSH 那边由插件直接给 turn 序号。
所以这里保留 `count_turns()`，判定本身全部交给 core。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _state import find_root, log, resolve_agent


def count_turns(transcript_path: str) -> int:
    """数用户消息条数，作为「这次会话干了多少事」的代理指标。"""
    if not transcript_path:
        return 0
    p = Path(transcript_path)
    if not p.is_file():
        return 0

    count = 0
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "user" or rec.get("role") == "user":
                    count += 1
    except Exception as exc:
        log(f"读 transcript 失败：{exc}")
    return count


def load_core():
    """按路径加载 core/memory.py。

    不用 `import memory`：core 目录一旦进 sys.path，这个名字容易和别处的
    memory 包撞；显式按路径加载更稳。
    """
    path = Path(__file__).resolve().parent.parent / "core" / "memory.py"
    spec = importlib.util.spec_from_file_location("memory_core", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        log(f"输入解析失败：{exc}")
        return 0

    root = find_root()
    if not root.is_dir():
        return 0

    agent, _ = resolve_agent(root, payload)
    cwd = payload.get("cwd") or os.environ.get("CODEBUDDY_PROJECT_DIR") or os.getcwd()
    turns = count_turns(str(payload.get("transcript_path", "")))
    session_id = str(payload.get("session_id", "")) or "unknown"

    try:
        core = load_core()
        result = core.digest_check(root, agent, cwd=cwd, turns=turns, session_id=session_id)
    except Exception as exc:  # 判定挂了不该挡住结束流程
        log(f"digest_check 失败：{exc}")
        return 0

    if result.get("should"):
        print(json.dumps({"continue": False, "stopReason": result["text"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
