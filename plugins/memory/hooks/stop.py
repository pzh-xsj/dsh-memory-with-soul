#!/usr/bin/env python3
"""Stop hook：每次响应结束时提醒两件事。

**触发频率**：`Stop` 每轮都跑，不是会话结束才跑一次。靠两道门控压住——
距上次提醒不足 `cooldownMinutes`（默认 360）就跳过，本会话超过
`maxPerSession`（默认 1）次也跳过。

**一、今天写了日志但没提炼**（头号问题）

这是「规则写了没有执行者」的典型：日志天天写，结论从不进项目册。
旧逻辑只在「距上次写入超 N 分钟」时提醒，它不知道「日志写了但没提炼」。

**二、记忆库久未写入**

唯一能兜住「当时觉得不重要、回头看很重要」的机制。

身份：agent_id = <persona>-<harness>。状态文件按 agent 拆开——`lastNudge`
如果全局一个，A 被提醒后 6 小时内 B 永远轮不到，提醒会被单向吞掉。

输出 continue=false + stopReason，让 agent 多跑一轮去写。允许「无新增」
解除，否则 agent 会为了关掉提醒硬编内容，那是污染。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _state import (
    extraction_state,
    find_project_book,
    find_root,
    last_write_ms,
    load_config,
    log,
    log_dir_for,
    read_state,
    read_text,
    resolve_agent,
    state_name,
    write_state,
)

MAX_SESSION_RECORDS = 20


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


def check_today_log(log_dir: Path) -> tuple[int, str] | None:
    """今天日志有结论但没交代提炼，返回 (结论条数, 状态)。"""
    p = log_dir / f"{date.today().isoformat()}.md"
    if not p.is_file():
        return None
    state, n = extraction_state(read_text(p))
    if n > 0 and state in {"missing", "none_bare"}:
        return n, state
    return None


def load_digest(root: Path, agent: str) -> dict:
    """读本 agent 的提醒状态；第一次生成时用旧的全局值播种。

    不播种的话，升级那一刻所有 agent 的 lastNudge 都是空的，
    会连环弹提醒——升级不该变成骚扰。
    """
    name = state_name(agent, "digest.json")
    state = read_state(root, name)
    if state:
        return state

    legacy = read_state(root, "digest.json")
    if legacy.get("lastNudge"):
        log(f"用旧全局 lastNudge 播种 {name}")
        return {"sessions": {}, "lastNudge": legacy["lastNudge"], "seeded_from": "digest.json"}
    return {}


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

    config = load_config(root)
    digest = config.get("digest", {})
    if not digest.get("enabled", True):
        return 0

    after_min = int(digest.get("afterMinutes", 180))
    min_turns = int(digest.get("minTurns", 3))
    cooldown_min = int(digest.get("cooldownMinutes", 360))
    max_per_session = int(digest.get("maxPerSession", 1))

    agent, _ = resolve_agent(root, payload)
    digest_name = state_name(agent, "digest.json")

    now = datetime.now()
    state = load_digest(root, agent)

    # 条件：冷却
    last_nudge = state.get("lastNudge", "")
    if last_nudge:
        try:
            if now - datetime.fromisoformat(last_nudge) < timedelta(minutes=cooldown_min):
                log(f"{agent} 冷却中，跳过")
                return 0
        except ValueError:
            pass

    # 条件：本会话提醒次数
    session_id = str(payload.get("session_id", "")) or "unknown"
    sessions = state.get("sessions", {})
    used = int(sessions.get(session_id, 0))
    if used >= max_per_session:
        return 0

    cwd = payload.get("cwd") or os.environ.get("CODEBUDDY_PROJECT_DIR") or os.getcwd()
    project = find_project_book(cwd)
    log_dir = log_dir_for(project, root, agent)

    # 第一优先：今天的日志有没有提炼
    pending = check_today_log(log_dir)
    if pending:
        n, st = pending
        why = "缺 `提炼：` 标记" if st == "missing" else "只写了「无」，没说理由"
        reason = (
            f"【提炼提醒】{agent} 今天的日志有 {n} 条结论，{why}。\n"
            "现在把结论提炼进项目册对应文件（`world.md` / `characters.md` / "
            "`playbooks/...`），然后在日志末尾补一行：\n"
            "`提炼：文件名` 或 `提炼：无（为什么不用提炼）`。\n"
            "只写「无」不算——那和没写分不出来。写完继续手头的事，不要开新任务。"
        )
        sessions[session_id] = used + 1
        if len(sessions) > MAX_SESSION_RECORDS:
            for key in list(sessions)[: len(sessions) - MAX_SESSION_RECORDS]:
                sessions.pop(key, None)
        state["sessions"] = sessions
        state["lastNudge"] = now.isoformat(timespec="seconds")
        state["agent"] = agent
        write_state(root, digest_name, state)
        print(json.dumps({"continue": False, "stopReason": reason}, ensure_ascii=False))
        log(f"{agent} 提炼提醒已发出（{n} 条结论，{st}）")
        return 0

    # 第二优先：记忆库多久没写
    lw = last_write_ms(root, project)
    if not lw:
        log("读不到写入时间，跳过")
        return 0
    stale_min = (now.timestamp() * 1000 - lw) / 60000
    if stale_min < after_min:
        log(f"记忆库 {stale_min:.0f} 分钟前刚写过，不提醒")
        return 0

    turns = count_turns(str(payload.get("transcript_path", "")))
    if turns < min_turns:
        log(f"会话只有 {turns} 轮，不提醒")
        return 0

    hours = stale_min / 60
    reason = (
        f"【记忆提醒】距上次写入记忆库已 {hours:.1f} 小时。\n"
        "如果这次会话有值得留下的东西——决定、结论、踩过的坑、改过的设定——"
        "现在写进项目册。没有的话，在当天日志记一条「无新增」并说明原因，"
        "这样同一段时间内不会再提醒。写完继续手头的事，不要开新任务。"
    )

    sessions[session_id] = used + 1
    if len(sessions) > MAX_SESSION_RECORDS:
        for key in list(sessions)[: len(sessions) - MAX_SESSION_RECORDS]:
            sessions.pop(key, None)
    state["sessions"] = sessions
    state["lastNudge"] = now.isoformat(timespec="seconds")
    state["agent"] = agent
    write_state(root, digest_name, state)

    print(json.dumps({"continue": False, "stopReason": reason}, ensure_ascii=False))
    log(f"{agent} 提醒已发出（{hours:.1f} 小时未写，{turns} 轮）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
