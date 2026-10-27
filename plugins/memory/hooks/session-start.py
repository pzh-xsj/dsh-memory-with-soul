#!/usr/bin/env python3
"""SessionStart hook：把记忆注入会话上下文。

WorkBuddy 的 SessionStart hook 会把本脚本的 stdout 直接加进上下文。
所以这里只输出记忆本身，不输出任何调试信息（调试走 stderr）。

注入分四块：
1. 告警（有才出现，放最前面——开头是补提炼成本最低的时候）
2. 保存协议（三行，写进上下文本身，不依赖 skill 是否加载）
3. 项目册入口 + 项目册最近日志（按 cwd 向上找）
4. 总册内容（config.json 的 inject.order）

身份：agent_id = <persona>-<harness>，例如 moxie-wb / moxie-dsh。
harness 靠 MEMORY_HARNESS → transcript_path → 插件路径判定。
payload 里没有 agent 字段，同一 harness 换专家认不出来，只能靠 MEMORY_AGENT 手工覆盖。

用法：
    python session-start.py            # 从 stdin 读 hook 输入（正常用法）
    python session-start.py --dry      # 不读 stdin，直接打印（手动测试）
    python session-start.py --dry --cwd "D:\\某项目"   # 指定 cwd 测试项目册定位

    python session-start.py --dry --agent moxie-dsh --role advisor   # 指定身份与角色卡
    python session-start.py --dry --subagent            # 按子 agent 的卡渲染（roles.subagent）
环境变量：
    MEMORY_DIR      记忆库路径，默认 ~/.memory
    MEMORY_AGENT    完整覆盖 agent id（逃生口，必须符合 [a-z0-9-]）
    MEMORY_HARNESS  强制 harness（wb / dsh）
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _state import (
    AGENT_RE,
    detect_harness,
    find_project_book,
    find_root,
    log,
    prune_sessions,
    read_text,
    recent_logs,
    remember_session,
    resolve_agent,
    resolve_role,
    role_card,
    touch_access,
    unextracted,
    write_state,
)

# 项目册入口最多占多少字符。入口是路由表，不是正文，给太多会挤掉 SOUL。
PROJECT_INJECT_MAX = 2250

# kind 等于 shared/playbooks/ 下的目录名。只允许这个形状，因为要拼进文件路径——
# 放开就等于允许 `kind: ../../etc` 穿越到记忆库外面。
KIND_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def resolve_cwd(payload: dict) -> tuple[str, str]:
    """找当前工作区目录，返回 (路径, 来源)。

    SessionStart 的 payload 里没有 cwd（实测只有 session_id/transcript_path 等），
    只能靠 CODEBUDDY_PROJECT_DIR。Stop 的 payload 反而有 cwd——两个 hook 不一样。
    """
    candidates = [
        ("payload.cwd", payload.get("cwd")),
        ("payload.workspace", payload.get("workspace")),
        ("env.CODEBUDDY_PROJECT_DIR", os.environ.get("CODEBUDDY_PROJECT_DIR")),
        ("env.CLAUDE_PROJECT_DIR", os.environ.get("CLAUDE_PROJECT_DIR")),
        ("env.WORKSPACE_FOLDER", os.environ.get("WORKSPACE_FOLDER")),
        ("env.PWD", os.environ.get("PWD")),
        ("os.getcwd", os.getcwd()),
    ]

    first = None
    for name, value in candidates:
        if not value:
            continue
        if first is None:
            first = (str(value), name)
        if find_project_book(value):
            return str(value), name
    return first or (os.getcwd(), "os.getcwd")


def fm_value(text: str, key: str) -> str:
    """从文件头部 frontmatter 取一个值，取不到返回空串。"""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    for line in text[3:end].splitlines():
        if line.strip().startswith(key + ":"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def tail_lines(text: str, n: int) -> str:
    """取最后 n 行。日志是 append-only，最新的在末尾。

    这一点很关键：用文件头会拿到最旧的记录（dsh-plugin-memory 踩过这个坑）。
    """
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def count_items(text: str) -> int:
    """数「## 未处理」下面有几条。

    `- ` 开头才算；占位行 `_（空）_` 不算，否则空 inbox 会永远显示「1 条」。
    """
    n = 0
    in_section = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_section = "未处理" in s
            continue
        if not in_section or not s.startswith("- "):
            continue
        rest = s[2:].strip()
        if rest and not rest.startswith("_"):
            n += 1
    return n


def load_project(project: Path, budget: int) -> str:
    """把项目册入口渲染成一段注入内容。"""
    meta_text = read_text(project / "_meta.md")
    title = fm_value(meta_text, "title") or project.parent.name
    status = fm_value(meta_text, "status") or "未标状态"

    index_path = project / "index.md"
    if not index_path.is_file():
        body = "_（项目册缺 index.md，跑一次 `memory.py index --write`）_"
    else:
        body = strip_frontmatter(read_text(index_path)).strip()
        first, _, rest = body.partition("\n")
        if first.strip().startswith("# "):
            body = rest.lstrip("\n")

    if len(body) > budget:
        body = body[:budget] + "\n\n_（项目册入口过长，已截断；全文见 `.memory/index.md`）_"

    return f"\n### 项目册 · {title}（{status}）\n\n{body.strip()}\n"


def load_project_log(project: Path, agent: str, n: int) -> str:
    """注入项目册里最近一份日志的尾部。

    建了日志却不注入，等于白建——下次会话接不上「上次干到哪」。
    取最近 3 天里最新的一份：周一开会话也能看到周五的记录。
    """
    logs = recent_logs(project / "log" / agent, 3)
    if not logs:
        return ""
    latest = logs[0]
    body = tail_lines(strip_frontmatter(read_text(latest)), n)
    if not body.strip():
        return ""
    return f"\n### 最近日志（{agent}，{latest.stem}）\n\n{body}\n"


def build_warning(project: Path | None, root: Path, agent: str) -> str:
    """未提炼提醒。有项目册看项目册，没有看总册的 agent 私有日志。"""
    if project:
        items = unextracted(project / "log" / agent, days=3)
    else:
        items = unextracted(root / "agents" / agent / "log", days=3)
    if not items:
        return ""

    parts = [f"{d} 的日志有 {n} 条结论未提炼" for d, n, _ in items]
    return (
        "提炼提醒："
        + "；".join(parts)
        + "。先补提炼——写进项目册对应文件，再在日志末尾加 `提炼：文件名` 或 `提炼：无（理由）`。"
    )


def load(
    root: Path,
    agent: str,
    config: dict,
    project: Path | None,
    warnings: list[str],
    harness: str,
    project_kind: str = "",
    role: str = "",
) -> tuple[str, list[str]]:
    inject = config.get("inject", {})
    budget = int(inject.get("maxChars", 12000))
    order = inject.get("order", [])
    today = date.today().isoformat()

    # kind 是项目册 → playbook 的唯一路由信号。空值或非法值一律当「没有」，静默跳过：
    # 它由 checkup 负责报，注入层报只会每次会话多一条噪声告警。
    kind = project_kind if project_kind and KIND_RE.fullmatch(project_kind) else ""

    items = []
    missing = []
    for entry in order:
        raw_path = entry.get("path", "")
        # 依赖 kind 的条目：没 kind 就不查文件，免得拼出 shared/playbooks//method.md
        if "{kind}" in raw_path and not kind:
            continue
        # 角色卡条目：没配角色就跳过，别拼出 roles//SOUL.md
        if "{role}" in raw_path and not role:
            continue
        path = raw_path.format(agent=agent, role=role, today=today, kind=kind)
        if "{" in path:
            # 配置里写了渲染器不认识的占位符。跳过，别拼出奇怪路径
            continue
        full = root / path
        # 迁移期兼容读：角色卡还没建时回退到旧位置 agents/<id>/SOUL.md。
        # 只保留一个版本——两处都能读会长期掩盖「迁移没做」。
        legacy_read = False
        if not full.is_file() and role and path == f"roles/{role}/SOUL.md":
            legacy = root / "agents" / agent / "SOUL.md"
            if legacy.is_file():
                full = legacy
                legacy_read = True
        if not full.is_file():
            # 必需文件缺失必须说出来。原来直接 continue，结果是静默注入空人格
            if entry.get("required"):
                missing.append(path)
            continue
        if legacy_read:
            warnings.append(
                f"人格读的是旧位置 agents/{agent}/SOUL.md；跑 `memory.py roles --migrate` 收口"
            )
        text = read_text(full)
        if not text.strip():
            if entry.get("required"):
                missing.append(path)
            continue

        if entry.get("route"):
            # 路由模式：只给「这是什么 + 什么时候读 + 全文在哪」，不注入正文。
            # 方法论全文动辄三四千字，塞进注入预算会把 focus 和当天日志挤掉。
            route_title = fm_value(text, "title") or full.stem
            summary = fm_value(text, "summary")
            when = fm_value(text, "when")
            chunks = [c for c in (summary, f"何时读：{when}" if when else "") if c]
            chunks.append(f"全文：{full.as_posix()}")
            items.append({
                "priority": int(entry.get("priority", 5)),
                "required": False,
                "route": True,
                "title": f"方法论路由 · {route_title}",
                "body": "\n\n".join(chunks),
                "path": path,
            })
            continue

        if entry.get("tailLines"):
            body = tail_lines(strip_frontmatter(text), int(entry["tailLines"]))
            if not body.strip():
                continue
        else:
            body = strip_frontmatter(text)

        # 只报条数不报内容（inbox）。内容一进上下文就变成第二个记忆库，
        # 而且它本来就是「暂时不知道放哪」的东西，不值得占注入预算。
        if entry.get("countOnly"):
            n = count_items(body)
            if n == 0:
                continue
            body = f"{n} 条待处理。超过 14 天 checkup 会报，要看内容直接开文件。"

        # 整文件预算（focus）。超了说明写成了流水账，截断只会留下半句话，
        # 不注入反而更干净——看不到焦点，人才会去改它。
        cap = entry.get("maxChars")
        if cap and len(body) > int(cap):
            warnings.append(
                f"{path} 超 {cap} 字符（实际 {len(body)}），整文件未注入。"
                "焦点是一行一句话，超了说明写成流水账了"
            )
            continue

        title = fm_value(text, "title") or full.stem

        # 正文自带的 H1 和注入标题重复，去掉一个
        body = body.strip()
        first, _, rest = body.partition("\n")
        if first.strip().startswith("# "):
            body = rest.lstrip("\n")

        items.append({
            "priority": int(entry.get("priority", 5)),
            "required": bool(entry.get("required", False)),
            "title": title,
            "body": body.strip(),
            "path": path,
        })

    items.sort(key=lambda x: (x["priority"], x["path"]))

    parts = []
    remaining = budget
    dropped = []
    truncated = []
    injected = []

    # 项目册排最前：它是当前任务上下文，比通用人格更相关
    if project:
        section = load_project(project, min(PROJECT_INJECT_MAX, max(0, budget // 2)))
        parts.append(section)
        # 必须 clamp：不 clamp 的话 remaining 会变负，后面「remaining > 300 就截断」
        # 这条分支永远进不去，所有条目直接判丢弃——一次小预算就把记忆全丢了。
        remaining = max(0, remaining - len(section))

        log_section = load_project_log(project, agent, 18)
        if log_section:
            parts.append(log_section)
            remaining = max(0, remaining - len(log_section))

    for item in items:
        header = f"\n### {item['title']}\n\n"
        cost = len(header) + len(item["body"])

        if item["required"] or item.get("route"):
            # 路由条目本来就几行，挤掉它省不下什么，反而丢掉「该用什么方法论」；
            # required 更不能丢。两者都不参与截断与丢弃。
            parts.append(header + item["body"])
            remaining = max(0, remaining - cost)
            injected.append(item["path"])
            continue

        if cost <= remaining:
            parts.append(header + item["body"])
            remaining -= cost
            injected.append(item["path"])
        elif remaining > 300:
            # 截断后按实际消耗扣预算。原来的写法把 remaining 硬置 0，
            # 一个大文件截断后，后面所有小文件全被丢掉。
            note = f"\n\n_（内容过长，已截断，原文 {len(item['body'])} 字）_"
            avail = max(0, remaining - len(header) - len(note))
            cut = item["body"][:avail]
            parts.append(header + cut + note)
            remaining = max(0, remaining - len(header) - len(cut) - len(note))
            injected.append(item["path"])
            truncated.append(item["path"])
        else:
            dropped.append(item["path"])

    if missing:
        # 空人格比未提炼严重，插到最前面
        warnings.insert(
            0,
            "缺少必需记忆文件："
            + "、".join(missing)
            + "（这次的人格是空的，先建好再继续）",
        )

    head = "<memory-context>\n"
    for w in warnings:
        head += f"⚠️ {w}\n"
    head += f"# 长期记忆（agent: {agent}，harness: {harness}，库：{root}）\n\n"
    head += (
        "以下是你的持久人格与记忆，先读完再回复。以它为准；"
        "若与当前对话冲突，优先相信主人最新的表述，并把差异写回记忆。\n"
    )
    head += (
        "\n"
        f"总册根：{root}    本项目册根：{project or '（无，本工作区未建项目册）'}\n"
        f"日志写这里：{(project / 'log' / agent) if project else (root / 'log' / agent)}\n"
        "保存：把结论写进项目册对应文件 → 项目 log 追加 `提炼：...` → 更新 index.md\n"
        "没写就等于这次结论只存在于对话里，下次会话读不到。\n"
    )
    if not project:
        head += (
            "本工作区还没有项目册。如果这里会长期干活（不只是临时看个文件），"
            "跑 `/memory-init` 建册：它会先问你三个问题，不会静默建。\n"
        )

    tail = "\n</memory-context>\n"
    notes = []
    if truncated:
        notes.append(f"因预算不足被截断：{', '.join(truncated)}")
    if dropped:
        notes.append(f"因预算不足未注入：{', '.join(dropped)}")
    if notes:
        tail = f"\n_（{'；'.join(notes)}。需要时用 `memory.py status` 查。）_\n" + tail

    return head + "".join(parts) + tail, injected


def main(argv: list[str]) -> int:
    dry = "--dry" in argv

    payload = {}
    if not dry:
        try:
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
                log(f"hook 输入：{payload.get('hook_event_name')} / client={payload.get('client')}")
        except Exception as exc:
            log(f"stdin 解析失败（继续执行）：{exc}")

    # --cwd / --agent / --role 只给手动测试与适配器用；正常路径从 payload / 环境变量里挑
    forced = None
    cli_agent = ""
    cli_role = ""
    subagent = "--subagent" in argv
    for i, arg in enumerate(argv):
        if arg == "--cwd" and i + 1 < len(argv):
            forced = argv[i + 1]
        elif arg.startswith("--cwd="):
            forced = arg.split("=", 1)[1]
        elif arg == "--agent" and i + 1 < len(argv):
            cli_agent = argv[i + 1].strip().lower()
        elif arg.startswith("--agent="):
            cli_agent = arg.split("=", 1)[1].strip().lower()
        elif arg == "--role" and i + 1 < len(argv):
            cli_role = argv[i + 1].strip()
        elif arg.startswith("--role="):
            cli_role = arg.split("=", 1)[1].strip()

    root = find_root()
    if not root.is_dir():
        log(f"记忆库不存在：{root}")
        return 0  # 静默退出，不阻塞会话

    config = {}
    config_path = root / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(read_text(config_path))
        except Exception as exc:
            log(f"config.json 解析失败：{exc}")

    agent, agent_src = resolve_agent(root, payload)
    harness, harness_src = detect_harness(payload)

    warnings = []
    if cli_agent:
        if AGENT_RE.match(cli_agent):
            agent, agent_src = cli_agent, "--agent"
        else:
            warnings.append(f"--agent 不合法（{cli_agent}），已忽略")
    if agent_src == "env.MEMORY_AGENT":
        warnings.append(f"MEMORY_AGENT 覆盖生效，本次身份是 {agent}（不是自动解析的）")
    if harness == "unknown":
        warnings.append(f"识别不出 harness，agent 定为 {agent}；确认没在别的环境里跑")

    # 角色：子 agent 查保留键 roles["subagent"]，普通会话查 roles[agent]
    role_agent = "subagent" if subagent else agent
    role, role_src = resolve_role(root, role_agent, config, cli_role)
    if subagent and role_src != "config.roles.subagent":
        warnings.append(
            "子 agent 没有单独配角色（roles.subagent），" + (f"沿用角色 {role}" if role else "defaultRole 也没配，这次没有可注入的人格")
        )

    if not role and not (root / "agents" / agent / "SOUL.md").is_file():
        warnings.append(
            f"没有可注入的人格：没配角色卡，也没有 agents/{agent}/SOUL.md"
            "（跑 `memory.py roles --migrate` 或手写 roles/<role>/SOUL.md）"
        )
    if role and not role_card(root, role).is_file() and not (root / "agents" / agent / "SOUL.md").is_file():
        warnings.append(f"角色卡不存在：roles/{role}/SOUL.md（这次人格是空的）")

    if forced:
        cwd, cwd_src = forced, "--cwd"
    else:
        cwd, cwd_src = resolve_cwd(payload)

    project = find_project_book(cwd)
    project_kind = ""
    if project:
        try:
            project_kind = fm_value(read_text(project / "_meta.md"), "kind")
        except OSError:
            project_kind = ""
    log(
        f"agent={agent}（{agent_src}）harness={harness}（{harness_src}）"
        f"role={role or '无'}（{role_src}）cwd={cwd} "
        f"项目册={project or '无'} kind={project_kind or '无'}"
    )

    # 会话 → agent 映射：post-read 的 payload 里没有 transcript_path，只能查这张表
    remember_session(root, str(payload.get("session_id", "")), agent, harness)
    prune_sessions(root, days=7)

    # 留一份诊断：hook 到底拿到了什么，下次会话能查。只写 .state，不动记忆内容
    write_state(root, f"session-start-{agent}.json", {
        "at": datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "agent_source": agent_src,
        "harness": harness,
        "harness_source": harness_src,
        "cwd": cwd,
        "cwd_source": cwd_src,
        "project": str(project) if project else None,
        "project_kind": project_kind,
        "role": role,
        "role_source": role_src,
        "subagent": subagent,
        "payload_keys": sorted(payload),
        "payload_client": payload.get("client"),
        "payload_agent_type": payload.get("agent_type"),
        "payload_agent_id": payload.get("agent_id"),
    })

    warn = build_warning(project, root, agent)
    if warn:
        warnings.insert(0, warn)

    output, injected = load(root, agent, config, project, warnings, harness, project_kind, role=role)
    sys.stdout.write(output)
    touch_access(root, injected, agent)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        # 渲染器崩了不能阻塞会话，但也不能静默：stderr 留痕，退出码给 0。
        # 没有这层保护时，任何一处 int()/路径异常都会让 hook 非零退出——
        # 会话照开，但这次一条记忆都没有，而且没人知道。
        log(f"渲染器异常，本次不注入：{type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.exit(0)
