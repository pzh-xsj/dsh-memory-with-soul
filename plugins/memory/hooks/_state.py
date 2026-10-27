#!/usr/bin/env python3
"""记忆库运行时状态的公共函数。

session-start.py / post-read.py / stop.py 共用，避免三份实现各自漂移。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def log(msg: str) -> None:
    """调试信息一律走 stderr。stdout 会被注入上下文，不能污染。"""
    print(f"[memory] {msg}", file=sys.stderr)


def find_root() -> Path:
    env = os.environ.get("MEMORY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".memory").resolve()


def find_project_book(start: Path | str | None = None) -> Path | None:
    """从 start 向上找含 `.memory/_meta.md` 的目录，返回该 `.memory` 的绝对路径。

    只认 `_meta.md`：空目录、只有 .memory 但没有 _meta.md 的，都不算项目册。
    这样误建一个空 .memory 不会让整个会话的记忆注入错位。
    """
    try:
        p = Path(start or os.getcwd()).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if p.is_file():
        p = p.parent
    for parent in [p, *p.parents]:
        candidate = parent / ".memory"
        if (candidate / "_meta.md").is_file():
            return candidate
    return None


# ---------------------------------------------------------------- 身份
#
# agent_id = <persona>-<harness>，例如 moxie-wb / moxie-dsh。
# 为什么带 harness：DSH 和 WorkBuddy 共用同一个 ~/.memory，如果都叫 moxie，
# 两边的人格、日志、冷却会互相踩。一个 agent 一间房。

AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SESSION_RE = re.compile(r"[^A-Za-z0-9._-]")

# 这些不是「人格」，是通用占位，不能拿来当 agent 名
AGENT_BLOCKLIST = {"default", "general-purpose", "general", "unknown", "none", "main"}


def normalize_persona(raw: object) -> str:
    """把任意来源的人格名洗成合法 agent id 片段。

    payload 的 agent_type 可能是显示名（含空格、中文、大写），直接拿来当目录名
    会出各种问题。洗不出来就用稳定短哈希兜底——宁可名字难看，也不能让两个
    专家静默合到一个目录里。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.lower() in AGENT_BLOCKLIST:
        return ""

    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    if s and AGENT_RE.match(s):
        return s

    # 中文 / 符号：洗不出来，用原文哈希保证唯一且稳定
    return "agent-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

HARNESS_MARKS = {
    "dsh": ("/.dsh/sessions/", "/.dsh/"),
    "wb": ("/.workbuddy-ai/projects/", "/.workbuddy-ai/"),
}


def detect_harness(payload: dict | None = None) -> tuple[str, str]:
    """判断当前跑在哪个 harness，返回 (harness, 来源)。

    优先级：环境变量 → 会话记录路径 → 插件自身路径 → unknown。
    transcript_path 用目录标记判断，不用子串 `.dsh`——工作区名里可能带这个字样。
    """
    env = os.environ.get("MEMORY_HARNESS", "").strip().lower()
    if env in HARNESS_MARKS:
        return env, "env.MEMORY_HARNESS"

    tp = str((payload or {}).get("transcript_path", "")).replace("\\", "/")
    for name, marks in HARNESS_MARKS.items():
        if marks[0] in tp:
            return name, "transcript_path"

    here = str(Path(__file__).resolve()).replace("\\", "/")
    for name, marks in HARNESS_MARKS.items():
        if marks[1] in here:
            return name, "plugin_path"

    return "unknown", "none"


def load_config(root: Path) -> dict:
    p = root / "config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(read_text(p))
    except Exception as exc:
        log(f"config.json 解析失败：{exc}")
        return {}


def resolve_agent(root: Path, payload: dict | None = None) -> tuple[str, str]:
    """解析当前 agent 的完整 id，返回 (agent_id, 来源)。

    优先级：
      1. MEMORY_AGENT            整体覆盖（逃生口），必须过白名单——否则 `../` 能写穿 .state
      2. payload.agent_type      专家/具名会话自带的身份（来源是 session.agentName）
      3. config.defaultAgent     普通会话
      4. default                 兜底

    第 2 条是「同一 harness 换专家」的唯一自动识别路径。harness 源码里
    buildAgentTrigger 会把它塞进 payload；普通会话没有这个字段，所以观察不到。
    """
    raw = os.environ.get("MEMORY_AGENT", "").strip().lower()
    if raw:
        if AGENT_RE.match(raw):
            return raw, "env.MEMORY_AGENT"
        log(f"MEMORY_AGENT 不合法（{raw}），已忽略")

    harness, _ = detect_harness(payload)

    persona = normalize_persona((payload or {}).get("agent_type"))
    source = "payload.agent_type"
    if not persona:
        persona = normalize_persona(load_config(root).get("defaultAgent"))
        source = "config.defaultAgent"
    if not persona:
        persona = "default"
        source = "fallback"

    if persona.endswith(f"-{harness}"):
        return persona, f"{source}（已含 harness）"
    return f"{persona}-{harness}", f"{source}+harness"


def state_name(agent: str, name: str) -> str:
    """access.json → access-moxie-wb.json。按 agent 拆文件，避免读改写互相覆盖。"""
    p = Path(name)
    return f"{p.stem}-{agent}{p.suffix}"


# ---------------------------------------------------------------- 角色卡
#
# 2026-10-27 起人格是「角色卡」：`roles/<role>/SOUL.md`。agent 只指向卡。
# 为什么独立出来：agent id 带 harness 后缀（moxie-wb / moxie-dsh），人格原来
# 长在 agents/<id>/ 里，于是同一个人在两台 harness 上是两份各自漂移的 SOUL.md。
#
# 放在 _state 而不是 core：渲染器（session-start）与 CLI 都要解析角色，
# 而 core 反过来依赖本文件——放这里两边都能用，且只有一份实现。

ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ROLE_DIR = "roles"
ROLE_CARD = "SOUL.md"
SUBAGENT_ROLE_KEY = "subagent"


def role_card(root: Path, role: str) -> Path:
    return root / ROLE_DIR / role / ROLE_CARD


def _fm_field(text: str, key: str) -> str:
    """从 frontmatter 原文里取一个键（只认单行 `key: value`）。"""
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def role_map(root: Path, cfg: dict | None = None) -> dict[str, str]:
    """config 里的 agent → role 映射。只留合法条目，坏数据不影响注入。"""
    raw = (cfg if cfg is not None else load_config(root)).get("roles") or {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for agent, role in raw.items():
        if isinstance(agent, str) and isinstance(role, str) and ROLE_RE.match(role):
            out[agent.strip().lower()] = role
    return out


def default_role(root: Path, cfg: dict | None = None) -> str:
    role = (cfg if cfg is not None else load_config(root)).get("defaultRole") or ""
    return role.strip() if isinstance(role, str) and ROLE_RE.match(role.strip()) else ""


def resolve_role(
    root: Path,
    agent: str,
    cfg: dict | None = None,
    override: str = "",
) -> tuple[str, str]:
    """解析 agent 该读哪张卡，返回 (role, 来源)。

    优先级：`--role` 传参 → roles[agent] → 子 agent 的 roles["subagent"]
    → defaultRole → 空（没有卡）。
    """
    cfg = cfg if cfg is not None else load_config(root)
    if override and ROLE_RE.match(override):
        return override, "cli"
    roles = role_map(root, cfg)
    if agent and agent in roles:
        return roles[agent], "config.roles"
    if agent == SUBAGENT_ROLE_KEY and SUBAGENT_ROLE_KEY in roles:
        return roles[SUBAGENT_ROLE_KEY], "config.roles.subagent"
    role = default_role(root, cfg)
    if role:
        return role, "config.defaultRole"
    return "", "none"


def list_roles(root: Path, cfg: dict | None = None) -> list[dict]:
    """所有角色卡 + 谁在用它。config 里写了但目录没建的也列出来——那正是要修的状态。"""
    cfg = cfg if cfg is not None else load_config(root)
    roles = role_map(root, cfg)
    folder = root / ROLE_DIR
    names = sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
    for role in roles.values():
        if role not in names:
            names.append(role)
    out = []
    for role in sorted(names):
        card = role_card(root, role)
        fm = card.read_text(encoding="utf-8") if card.is_file() else ""
        out.append({
            "role": role,
            "has_soul": card.is_file(),
            "name": _fm_field(fm, "name"),
            "identity": _fm_field(fm, "identity"),
            "agents": sorted(a for a, r in roles.items() if r == role),
        })
    return out


def session_key(session_id: str) -> str:
    s = SESSION_RE.sub("_", str(session_id or ""))[:80]
    return s or "unknown"


def remember_session(root: Path, session_id: str, agent: str, harness: str) -> None:
    """会话 → agent 映射。一个会话一个文件，并发写天然不冲突。

    post-read 的 payload 里没有 transcript_path，拿不到 harness，
    只能靠这张表查回是哪个 agent。
    """
    sid = session_key(session_id)
    if sid == "unknown":
        return
    write_state(root, f"sessions/{sid}.json", {
        "agent": agent,
        "harness": harness,
        "at": datetime.now().isoformat(timespec="seconds"),
    })


def session_agent(root: Path, session_id: str) -> str | None:
    sid = session_key(session_id)
    if sid == "unknown":
        return None
    data = read_state(root, f"sessions/{sid}.json")
    agent = str(data.get("agent", "") or "")
    return agent if AGENT_RE.match(agent) else None


def prune_sessions(root: Path, days: int = 7) -> None:
    """清掉过期会话映射。一个会话一个文件，不清理会一直涨。"""
    folder = root / ".state" / "sessions"
    if not folder.is_dir():
        return
    cutoff = datetime.now().timestamp() - days * 86400
    for p in folder.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue


# ---------------------------------------------------------------- 提炼判据
#
# 为什么要四个状态：日志模板的默认值就是「提炼：无」。如果一律把「无」判成
# 未提炼，规范书写反而永久报警，检查会被当噪音忽略——那就回到原点了。

EXTRACT_RE = re.compile(r"^\s*提炼\s*[:：]\s*(.*)$")
CONCL_HEAD_RE = re.compile(r"^#{2,4}\s*结论\s*$")
ANY_HEAD_RE = re.compile(r"^#{1,4}\s")
ITEM_RE = re.compile(r"^([-*+]|\d+[.)])\s+")


def count_conclusions(text: str) -> int:
    """数最后一个「## 结论」段里的条目数。没有这段返回 0。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if CONCL_HEAD_RE.match(line.strip()):
            start = i
    if start is None:
        return 0

    n = 0
    has_text = False
    for line in lines[start + 1:]:
        if ANY_HEAD_RE.match(line.strip()):
            break
        s = line.strip()
        if not s or s.startswith("**"):
            continue
        has_text = True
        if ITEM_RE.match(s):
            n += 1
    return n or (1 if has_text else 0)


def extraction_state(text: str) -> tuple[str, int]:
    """判断日志的提炼状态。返回 (状态, 结论条数)。

    状态：
      done       已提炼，标记里写了目标文件
      none_ok    已判定无需提炼（`提炼：无（理由）`）
      none_bare  写了「无」但没给理由，等同于没交代
      missing    没有标记，或标记为空
    """
    last = None
    for line in text.splitlines():
        m = EXTRACT_RE.match(line)
        if m:
            last = m.group(1).strip()

    n = count_conclusions(text)
    if last is None or not last:
        return "missing", n
    if last.startswith("无"):
        rest = last[1:].strip()
        if rest.startswith(("（", "(")) and rest.endswith(("）", ")")) and len(rest) > 2:
            return "none_ok", n
        return "none_bare", n
    return "done", n


def recent_logs(log_dir: Path, days: int = 3) -> list[Path]:
    """最近 days 天（含今天）存在的日志，按日期倒序。"""
    if not log_dir.is_dir():
        return []
    today = date.today()
    out = []
    for i in range(days):
        p = log_dir / f"{(today - timedelta(days=i)).isoformat()}.md"
        if p.is_file():
            out.append(p)
    return out


def log_dir_for(project: Path | None, root: Path, agent: str) -> Path:
    """日志目录只有这一处定义，避免三份代码各自拼路径漂移。

    有项目册写项目册，没有才退回总册的 agent 私有日志。
    注意：调用方必须把 agent 带上（`log/<agent>`）。只传 `log` 不会报错，
    只会静默返回空——未提炼提醒就永远不响了。
    """
    if project:
        return project / "log" / agent
    return root / "agents" / agent / "log"


def unextracted(log_dir: Path, days: int = 3) -> list[tuple[str, int, str]]:
    """扫最近几天日志，返回 [(日期, 结论条数, 状态)]，只含没交代的。

    `提炼：无（理由）` 不算没交代——那是已判定。
    """
    out = []
    for p in recent_logs(log_dir, days):
        state, n = extraction_state(read_text(p))
        if n == 0:
            continue
        if state in {"missing", "none_bare"}:
            out.append((p.stem, n, state))
    return out


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        log(f"读不了 {path}: {exc}")
        return ""


def read_state(root: Path, name: str) -> dict:
    p = root / ".state" / name
    if not p.is_file():
        return {}
    try:
        return json.loads(read_text(p))
    except Exception as exc:
        log(f"状态文件 {name} 解析失败：{exc}")
        return {}


def write_state(root: Path, name: str, data: dict) -> None:
    """原子写状态文件。

    为什么必须原子：两个会话并发时，`write_text` 先截断再写，另一个进程读到的
    可能是半截 JSON，解析失败后整张表归零（access.json 尤其致命）。

    tmp 名必须带 pid+uuid：固定后缀会让两个进程共用同一个临时文件，
    换上去的是别人写了一半的内容，比截断更难查。
    """
    p = root / ".state" / name
    tmp = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(text, encoding="utf-8")
        for attempt in (1, 2):
            try:
                os.replace(tmp, p)
                return
            except OSError as exc:
                if attempt == 2:
                    raise
                log(f"原子替换失败，重试一次：{exc}")
    except Exception as exc:
        log(f"写状态文件 {name} 失败（旧文件保留）：{exc}")
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def touch_access(root: Path, paths: list[str], agent: str | None = None) -> None:
    """把本次读到的文件戳成今天。

    按天幂等：同一天重复读不重复写。
    状态放 .state/access-<agent>.json，不写进记忆文件本身——否则每次读都产生
    git 变更，真正的内容改动会被 diff 淹没。

    按 agent 分文件是为了避免并发读改写：两个会话同时戳记时，后写者会把
    前者的整张表覆盖掉。
    """
    if not paths:
        return

    name = state_name(agent, "access.json") if agent else "access.json"
    today = date.today().isoformat()
    state = read_state(root, name)
    files = state.setdefault("files", {})

    changed = False
    for path in paths:
        if files.get(path) != today:
            files[path] = today
            changed = True

    if not changed:
        return

    state["updated"] = datetime.now().isoformat(timespec="seconds")
    write_state(root, name, state)


def relative_to_root(root: Path, file_path: str) -> str | None:
    """绝对路径 → 记忆库内的相对 POSIX 路径。不在库里返回 None。"""
    if not file_path:
        return None
    try:
        p = Path(file_path).expanduser().resolve()
        rel = p.relative_to(root)
    except (ValueError, OSError):
        return None
    if not rel.parts or rel.parts[0] in {".state", ".git"}:
        return None
    return rel.as_posix()


def last_write_ms(root: Path, project: Path | None = None) -> int:
    """最后一次内容写入的时间戳（毫秒）。

    取 git 索引和各内容目录里最新 mtime 的最大值。
    用 git 的 index mtime 而不是工作区文件的 mtime：后者会被编辑器、
    同步盘、备份工具随便碰，前者只在真正 add/commit 时变。

    project 传项目册根：项目日志写在项目册里，不扫它的话 Stop hook 会
    永远看不到「刚写过」，每次都误判成「很久没写」。
    """
    candidates = []

    bases = [root]
    if project and project.is_dir():
        bases.append(project)

    for base in bases:
        git_index = base / ".git" / "index"
        if git_index.is_file():
            try:
                candidates.append(int(git_index.stat().st_mtime * 1000))
            except OSError:
                pass

        for sub in ("shared", "agents", "log"):
            folder = base / sub
            if not folder.is_dir():
                continue
            for p in folder.rglob("*.md"):
                try:
                    candidates.append(int(p.stat().st_mtime * 1000))
                except OSError:
                    continue

    return max(candidates) if candidates else 0
