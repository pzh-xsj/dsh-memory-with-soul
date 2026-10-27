#!/usr/bin/env python3
"""记忆库 CLI —— 平台无关的核心。

不依赖任何第三方库，任何装了 Python 3.8+ 的机器都能跑。
平台适配器（WorkBuddy hook、将来的其他平台）只负责调用这里的命令，
数据格式和业务逻辑全在本文件。

用法：
    python memory.py status              看总册状态
    python memory.py status --all        跨所有项目册汇总
    python memory.py checkup             体检，输出行动清单
    python memory.py checkup --all       每个项目册都体检
    python memory.py index               看索引是否漂移
    python memory.py index --write       重写索引三张表（保留手写行）
    python memory.py workspaces          列登记的项目册
    python memory.py workspaces --fix <id> <新路径>    路径丢了按 id 重绑
    python memory.py timeline [--days N] 跨项目册日志时间线（默认 30 天）
    python memory.py digest --agent <id> [--cwd 路径] [--turns N] [--session <id>] [--json]
                                         判断现在要不要提醒提炼（命中退出码 2，stdout 是文案）
    python memory.py track --paths <文件>  把读过的记忆文件戳成今天（DSH 的 PostToolUse 调）
    python memory.py skills [--install] [--mode copy|link]
                                         把插件自带的 skill 装进 ~/.dsh/skills/
    python memory.py init [路径]         初始化新库
    python memory.py init --agent <id> [--from <id>]        建一个 agent 的私有目录
    python memory.py init --project <路径> [--id <id>] [--title <标题>] [--kind <领域>]
                          kind 决定注入哪个 playbook，取值见 `status` 输出
                                                            建一个项目册并登记

2026-10-26 重构：`shared/domains/` 拆掉之后，可枚举的东西从「五个分册」
变成「playbooks + topics + 项目册」三类。本文件按这个新结构重写扫描层。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta
from hashlib import sha1
from pathlib import Path

# Windows 控制台默认 GBK，中文会乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 提炼判据只在 hooks/_state.py 里实现一份，CLI 直接复用，不复制。
# 复制两份必然漂移——这个项目已经为「三份实现各自漂移」付过代价。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
try:
    from _state import (
        extraction_state,
        find_project_book,
        last_write_ms,
        load_config,
        log,
        log_dir_for,
        read_state,
        read_text,
        relative_to_root,
        resolve_agent,
        session_agent,
        state_name,
        touch_access,
        unextracted,
        write_state,
    )
except Exception:  # 单独拷走 core/ 时仍能跑，只是少了提炼检查
    extraction_state = None
    find_project_book = None
    last_write_ms = None
    load_config = None
    log = None
    log_dir_for = None
    read_state = None
    read_text = None
    relative_to_root = None
    resolve_agent = None
    session_agent = None
    state_name = None
    touch_access = None
    unextracted = None
    write_state = None

# ---------------------------------------------------------------- 常量

# 判断 summary 是否在说空话。命中任何一个就算不合格。
VAGUE_WORDS = ["相关内容", "各种信息", "各类信息", "等等", "一些", "若干", "其他内容"]

# playbooks / topics 的每个文件必须有的元信息字段
REQUIRED_FIELDS = ["title", "summary", "when", "owner"]

# 项目册 _meta.md 必须有的字段
PROJECT_FIELDS = ["id", "title", "status", "kind"]

# kind 的合法形状：小写、数字、连字符，1-32 位。它就是 shared/playbooks/ 下的目录名，
# 所以这个正则同时是防目录穿越的口子——`../`、大写、空格一律不认。
KIND_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# 内置 kind → 显示名。没列出来的自定义 kind 直接显示原名（不拦）。
KIND_NAMES = {
    "engineering": "工程",
    "research": "科研",
    "writing": "写作",
    "general": "通用",
}

# 定位段最长字数
MAX_POSITION_CHARS = 40

# 项目册多久没动过就提示（只提示，不自动归档——长期项目会误伤）
PROJECT_IDLE_DAYS = 30

# focus.md 的四条硬约束
FOCUS_MAX_ITEMS = 5      # 第 6 条进来必须先删一条
FOCUS_MAX_CHARS = 600    # 注入预算，超了整文件不注入（2026-10-26 随 maxChars 8000→12000 等比放宽）
FOCUS_STALE_DAYS = 7     # 一条超过 7 天没进展就该更新或删

# inbox.md 的积压阈值
INBOX_STALE_DAYS = 14

# 项目册 index.md 的「最近变更」上限。它是注入的项目册入口（≤2250 字符），
# 不设上限就会无限长，最后只能靠调大 maxChars 止血。
RECENT_CHANGES_MAX = 8

# 索引里由程序维护的三张表。标题按前缀匹配，后面的括号说明可以随便改。
# 顺序就是写进 index.md 的顺序。
INDEX_SECTIONS = ("项目册", "playbooks", "topics")

# config.json 的默认值 —— **只写这一份**。`init` 建库用它，设置页回退值也从这里拿。
# 2026-10-27 之前 UI 里手抄了一份（afterMinutes 写 240，真实配置是 180），
# 两边对不上：页面显示 240，跑起来按 180 算。
CONFIG_DEFAULTS = {
    "schema": 3,
    "defaultAgent": "default",
    "inject": {"maxChars": 12000, "order": []},
    "digest": {
        "enabled": True,
        "afterMinutes": 180,
        "minTurns": 3,
        "cooldownMinutes": 360,
        "maxPerSession": 1,
    },
    "archive": {"staleDays": 180},
    "sync": {"enabled": False, "remote": ""},
}


# ---------------------------------------------------------------- 基础

def find_root() -> Path:
    """定位记忆库。顺序：环境变量 → ~/.memory"""
    env = os.environ.get("MEMORY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".memory").resolve()


def default_agent(root: Path) -> str:
    """当前 agent 的完整 id（<persona>-<harness>）。

    优先走 hooks/_state 的解析，保证 CLI 和 hook 口径一致；
    没有 _state 时退回读 config 的 defaultAgent。
    """
    if resolve_agent:
        try:
            agent, _ = resolve_agent(root, {})
            return agent
        except Exception:
            pass
    p = root / "config.json"
    if p.is_file():
        try:
            return json.loads(read(p)).get("defaultAgent", "default")
        except Exception:
            pass
    return "default"


def list_agents(root: Path) -> list[str]:
    """总册里所有 agent。带 .legacy 标记的废弃目录不算。"""
    agents = root / "agents"
    if not agents.is_dir():
        return []
    out = []
    for p in sorted(agents.iterdir()):
        if not p.is_dir() or (p / ".legacy").exists():
            continue
        out.append(p.name)
    return out


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    """原子写：先写同目录的临时文件，再 `os.replace` 换过去。

    非原子写的后果不是「写坏一点」，是整个文件被截成半截——`index --write`
    会整文件重写 `shared/index.md`，中途断一次索引就废了。
    临时文件放同目录，是因为跨盘 `os.replace` 不保证原子。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def parse_frontmatter(path: Path) -> dict:
    """解析文件头部的 --- 元信息。只支持 key: value 这种单行格式。

    故意不引入 PyYAML：记忆库要能在任何机器上跑，少一个依赖少一个坑。
    """
    try:
        text = read(path)
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    data = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        if value:
            data[key.strip()] = value
    return data


def body_of(path: Path) -> str:
    """取 frontmatter 之后的正文。"""
    text = read(path)
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def run_git(repo: Path, *args: str) -> str:
    """在指定仓库里跑 git 命令，失败返回空串（不抛异常）。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def slugify(name: str) -> str:
    """把目录名洗成合法的 id。中文洗不出来时用哈希兜底。

    宁可难看，也不能让两个项目静默撞 id。
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if s and s[0].isdigit():
        s = "p-" + s
    if s:
        return s
    return "p-" + sha1(name.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------- 扫描

def list_md(folder: Path) -> list[Path]:
    """列一个目录下的 md，排除骨架文件和 README。"""
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("*.md")):
        if p.name in {"index.md", "README.md"} or p.name.startswith("_"):
            continue
        out.append(p)
    return out


def scan_md(path: Path, root: Path, domain: str = "") -> dict:
    """读一个记忆文件的元信息。

    `rel` 是相对记忆库根的 POSIX 路径，和 PostToolUse 记的访问戳记同一个格式——
    归档候选就靠它对齐，格式差一个字符就永远匹配不上。
    `display` 是相对 shared/ 的短路径，给索引表用。
    """
    fm = parse_frontmatter(path)
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        rel = path.as_posix()
    try:
        display = path.resolve().relative_to(root / "shared").as_posix()
    except (ValueError, OSError):
        display = rel
    return {
        "rel": rel,
        "display": display,
        "path": path,
        "name": path.name,
        "stem": path.stem,
        "domain": domain,
        "title": fm.get("title", ""),
        "summary": fm.get("summary", ""),
        "when": fm.get("when", ""),
        "group": fm.get("group", ""),
        "owner": fm.get("owner", ""),
    }


def list_playbooks(root: Path) -> list[dict]:
    """`shared/playbooks/<领域>/<类型>.md` 下的全部文件。

    两维结构（领域 + 类型）是拍过板的：一个 taste.md 混装写作和科研，
    `when` 写不出来。
    """
    base = root / "shared" / "playbooks"
    if not base.is_dir():
        return []
    out = []
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in list_md(folder):
            out.append(scan_md(f, root, domain=folder.name))
    return out


def playbook_kinds(root: Path) -> set[str]:
    """`shared/playbooks/` 下实际存在的领域目录名。

    这就是 kind 的取值域：kind 与目录一一对应，加一个领域 = 建一个目录，
    不需要改代码。渲染器注入时按 `playbooks/{kind}/method.md` 找文件，找不到就跳过。
    """
    base = root / "shared" / "playbooks"
    if not base.is_dir():
        return set()
    return {p.name for p in base.iterdir() if p.is_dir()}


def kind_label(kind: str) -> str:
    """kind 是空的就说清楚空着会怎样，别只印一个空字符串。"""
    if not kind:
        return "（未设置：不会注入任何 playbook）"
    if not KIND_RE.fullmatch(kind):
        return f"{kind}（格式不合法：应为小写字母/数字/连字符）"
    if kind not in KIND_NAMES:
        return f"{kind}（自定义）"
    return f"{kind}（{KIND_NAMES[kind]}）"


def list_topics(root: Path) -> list[dict]:
    """`shared/topics/<主题>.md`。单文件主题，不建分册。"""
    base = root / "shared" / "topics"
    return [scan_md(f, root) for f in list_md(base)]


def workspaces_path(root: Path) -> Path:
    return root / "workspaces.json"


def read_workspaces(root: Path) -> list[dict]:
    """读注册表。只存 id → path，别的一律不存。

    定位靠 `cwd` 向上找 `.memory/_meta.md`，注册表只管「一共有哪些」——
    两个不同问题。漏登记只让总表少一行，不影响记忆可用。
    """
    p = workspaces_path(root)
    if not p.is_file():
        return []
    try:
        data = json.loads(read(p))
    except Exception:
        return []
    items = data.get("workspaces", [])
    return [i for i in items if isinstance(i, dict) and i.get("path")]


def write_workspaces(root: Path, items: list[dict]) -> None:
    """写注册表。路径统一成正斜杠——反斜杠在 JSON 里要转义，
    手改一次就多一层 `\\\\`，迟早有人改坏。"""
    norm = []
    for item in items:
        d = dict(item)
        if d.get("path"):
            d["path"] = Path(str(d["path"])).as_posix()
        norm.append(d)
    write(
        workspaces_path(root),
        json.dumps({"version": 1, "workspaces": norm}, ensure_ascii=False, indent=2) + "\n",
    )


def scan_project(ws: dict) -> dict:
    """读一个已登记项目的实际状态。

    `state`（ok/missing）和 `status`（进行中/搁置）**不能合并**：
    路径丢了不代表项目废弃，混用会误归档丢数据。
    """
    path = Path(ws.get("path", "")).expanduser()
    book = path / ".memory"
    meta_path = book / "_meta.md"
    exists = meta_path.is_file()
    meta = parse_frontmatter(meta_path) if exists else {}

    last = ""
    if path.is_dir():
        try:
            last = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        except OSError:
            last = ""

    return {
        "id": meta.get("id") or ws.get("id", ""),
        "path": path,
        "book": book,
        "state": "ok" if exists else "missing",
        "title": meta.get("title", ""),
        "status": meta.get("status", ""),
        "kind": meta.get("kind", ""),
        "position": extract_position(meta_path) if exists else "",
        "last_active": last,
        "meta": meta,
    }


def extract_position(meta_path: Path) -> str:
    """从 `_meta.md` 的「## 定位」段取第一行正文。

    占位符不算定位：`_待补：..._` 这类是模板留下的坑，把它当成内容
    会让 checkup 误判「有定位」，也会把占位符写进索引表。
    """
    try:
        text = body_of(meta_path)
    except OSError:
        return ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("## 定位"):
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    return ""
                if s.startswith("_") and ("待补" in s or s.strip("_") == ""):
                    return ""
                return s
            return ""
    return ""


# ---------------------------------------------------------------- 项目册：可编辑字段

# status 的合法取值。册子里可能写着别的词（历史遗留），编辑时原值照旧放行——
# 不让一次「打开再保存」把旧册子改成非法值。
EDITABLE_STATUS = ("进行中", "待启动", "搁置")

# 「注意事项」里的三块，顺序就是渲染顺序。
NOTE_BLOCKS = (
    ("禁区", "forbidden"),
    ("前置动作", "pre_actions"),
    ("读取提示", "hints"),
)

# 定位的硬上限。checkup 在 MAX_POSITION_CHARS（40）就提示，但不拦；
# 这里留更宽的口子，免得「一句话说不完」把编辑卡死。
POSITION_HARD_LIMIT = 120

# 单个注意事项块的硬上限
NOTE_HARD_LIMIT = 2000

BLOCK_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")


class MetaError(Exception):
    """可编辑字段的校验错误。code 映射到 HTTP：invalid 400 / conflict 409 / missing 404。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _meta_parts(text: str) -> tuple[str, str]:
    """切成 (frontmatter 原文，含结尾的 `---`；正文)。没有 frontmatter 时前者为空串。"""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    return text[: end + 4], text[end + 4:].lstrip("\n")


def _join_meta(fm: str, body: str) -> str:
    """frontmatter + 空行 + 正文。只走这一个拼接口，格式才稳定（连写两次不会多空行）。"""
    return fm + "\n\n" + body.strip("\n") + "\n"


def _set_fm(fm_text: str, key: str, value: str) -> str:
    """只改 frontmatter 里的一行；没有这个键就插在结尾 `---` 之前。"""
    lines = fm_text.split("\n")
    pat = re.compile(r"^" + re.escape(key) + r":")
    for i in range(1, len(lines)):
        if pat.match(lines[i].strip()):
            lines[i] = f"{key}: {value}"
            return "\n".join(lines)
    if lines and lines[-1].strip() == "---":
        lines.insert(len(lines) - 1, f"{key}: {value}")
    else:
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _section_inner(body: str, title: str) -> str | None:
    """取 `## title` 到下一个二级标题之间的正文。没有这一节返回 None。"""
    lines = body.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {title}":
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s", lines[j]):
            end = j
            break
    return "\n".join(lines[start + 1:end]).strip("\n")


def _replace_section(body: str, title: str, inner: str) -> str:
    """替换（或补上）一个二级段落。其它段落一个字节都不动。"""
    lines = body.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {title}":
            start = i
            break
    if start is None:
        body = body.rstrip("\n")
        head = f"## {title}\n\n{inner.strip()}\n"
        return (body + "\n\n" + head) if body else head
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s", lines[j]):
            end = j
            break
    before = "\n".join(lines[: start + 1]).rstrip("\n")
    after = "\n".join(lines[end:]).strip("\n")
    out = before + "\n\n" + inner.strip() + "\n"
    if after:
        out += "\n" + after + "\n"
    return out


def _parse_note_blocks(inner: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """把「注意事项」切成 (块前文字, [(块名, 块正文行)])。

    块名就是 `**禁区**` 这种加粗行。块正文原样保留，编辑只换正文。
    """
    pre: list[str] = []
    blocks: list[list] = []
    for line in inner.split("\n"):
        m = BLOCK_RE.match(line.strip())
        if m:
            blocks.append([m.group(1), []])
        elif blocks:
            blocks[-1][1].append(line)
        else:
            pre.append(line)
    return pre, [(t, b) for t, b in blocks]


def _render_note_blocks(pre: list[str], blocks: list[tuple[str, list[str]]]) -> str:
    out = []
    pre_text = "\n".join(pre).strip("\n")
    if pre_text.strip():
        out.append(pre_text)
    for title, body in blocks:
        body_text = "\n".join(body).strip("\n")
        out.append(f"**{title}**" + (f"\n\n{body_text}" if body_text.strip() else ""))
    return "\n\n".join(out)


def read_project_meta(meta_path: Path) -> dict:
    """读一本项目册 `_meta.md` 里「允许在 UI 改」的字段。"""
    meta_path = meta_path.resolve()
    if not meta_path.is_file():
        raise MetaError("missing", "找不到 _meta.md")
    text = read(meta_path)
    fm_text, body = _meta_parts(text)
    fm = parse_frontmatter(meta_path)
    pre, blocks = _parse_note_blocks(_section_inner(body, "注意事项") or "")
    notes = {name: "\n".join(lines).strip("\n") for name, lines in blocks}
    out = {
        "path": meta_path.parent.parent.as_posix(),
        "id": fm.get("id", ""),
        "title": fm.get("title", ""),
        "status": fm.get("status", ""),
        "kind": fm.get("kind", ""),
        "workspace": fm.get("workspace", ""),
        "position": (_section_inner(body, "定位") or "").strip(),
        "mtime": meta_path.stat().st_mtime,
        "has_frontmatter": bool(fm_text),
    }
    for name, key in NOTE_BLOCKS:
        out[key] = notes.get(name, "")
    return out


def write_project_meta(meta_path: Path, fields: dict, expect_mtime: float | None = None) -> dict:
    """只改 `_meta.md` 的 status / kind / title / 定位 / 注意事项三块。

    **不整体重序列化**：没提到的字节原样保留（空行、created、正文其它段落）。
    """
    meta_path = meta_path.resolve()
    if not meta_path.is_file():
        raise MetaError("missing", "找不到 _meta.md")
    old_mtime = meta_path.stat().st_mtime
    if expect_mtime is not None:
        try:
            if abs(old_mtime - float(expect_mtime)) > 0.0005:
                raise MetaError("conflict", "文件在打开后被改过，先重新加载再保存")
        except (TypeError, ValueError):
            pass

    text = read(meta_path)
    fm_text, body = _meta_parts(text)
    if not fm_text:
        raise MetaError("invalid", "_meta.md 没有 frontmatter，先手工补或跑 init --project")
    fm = parse_frontmatter(meta_path)
    changed: list[str] = []

    def pick(key: str) -> str | None:
        if key not in fields:
            return None
        return "" if fields[key] is None else str(fields[key]).strip()

    status = pick("status")
    if status is not None:
        if not status:
            raise MetaError("invalid", "status 不能为空")
        if status not in EDITABLE_STATUS and status != fm.get("status", ""):
            raise MetaError("invalid", "status 只能是 " + " / ".join(EDITABLE_STATUS))
        if status != fm.get("status", ""):
            fm_text = _set_fm(fm_text, "status", status)
            changed.append("status")

    kind = pick("kind")
    if kind is not None:
        if kind and not KIND_RE.fullmatch(kind):
            raise MetaError("invalid", "kind 只能是小写字母/数字/连字符，1-32 位")
        if kind != fm.get("kind", ""):
            fm_text = _set_fm(fm_text, "kind", kind)
            changed.append("kind")

    title = pick("title")
    if title is not None:
        if not title:
            raise MetaError("invalid", "title 不能为空")
        if "\n" in title:
            raise MetaError("invalid", "title 不能换行")
        if title != fm.get("title", ""):
            fm_text = _set_fm(fm_text, "title", title)
            changed.append("title")

    position = pick("position")
    if position is not None:
        if len(position) > POSITION_HARD_LIMIT:
            raise MetaError("invalid", f"定位 {len(position)} 字，上限 {POSITION_HARD_LIMIT}")
        old_pos = (_section_inner(body, "定位") or "").strip()
        if position != old_pos:
            body = _replace_section(body, "定位", position)
            changed.append("position")

    note_updates = [(name, key) for name, key in NOTE_BLOCKS if key in fields]
    if note_updates:
        inner = _section_inner(body, "注意事项") or ""
        pre, blocks = _parse_note_blocks(inner)
        fresh = {name: ("" if fields.get(key) is None else str(fields[key]).strip())
                 for name, key in note_updates}
        new_blocks: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
        note_dirty = False
        for name, lines in blocks:
            body_lines = lines
            if name in fresh:
                body_lines = fresh[name].split("\n") if fresh[name] else []
                seen.add(name)
                old_text = "\n".join(lines).strip("\n")
                if fresh[name].strip() != old_text:
                    changed.append(next(k for n, k in note_updates if n == name))
                    note_dirty = True
            new_blocks.append((name, body_lines))
        # 文件里缺的块补在末尾（按 NOTE_BLOCKS 的顺序），空值不补
        for name, key in note_updates:
            if name in seen or not fresh[name]:
                continue
            new_blocks.append((name, fresh[name].split("\n")))
            changed.append(key)
            note_dirty = True
        # 只有注意事项真的变了才重排这一节：手写的空行不该被「只改了 status」顺手抹平
        if note_dirty:
            body = _replace_section(body, "注意事项", _render_note_blocks(pre, new_blocks))

    if not changed:
        return {
            "path": meta_path.parent.parent.as_posix(),
            "changed": [],
            "mtime": old_mtime,
            "git_dirty": bool(run_git(meta_path.parent, "status", "--porcelain")),
        }

    fm_text = _set_fm(fm_text, "updated", date.today().isoformat())
    write(meta_path, _join_meta(fm_text, body))
    return {
        "path": meta_path.parent.parent.as_posix(),
        "changed": changed,
        "mtime": meta_path.stat().st_mtime,
        "git_dirty": bool(run_git(meta_path.parent, "status", "--porcelain")),
    }


# ---------------------------------------------------------------- UI 视图（/api 用）

def project_books(root: Path) -> list[dict]:
    """登记的项目册 + 实际状态。口径和 `checkup` 一致，给设置页渲染表格。"""
    kinds = playbook_kinds(root)
    out = []
    for ws in read_workspaces(root):
        p = scan_project(ws)
        book = p["book"]
        out.append({
            "id": p["id"],
            "path": p["path"].as_posix(),
            "book": book.as_posix(),
            "state": p["state"],
            "title": p["title"],
            "status": p["status"],
            "kind": p["kind"],
            "kind_ok": bool(p["kind"]) and p["kind"] in kinds,
            "position": p["position"],
            "last_active": p["last_active"],
            "git_dirty": bool(run_git(book, "status", "--porcelain")) if p["state"] == "ok" else False,
        })
    return out


_SESSION_START = None


def _session_start_module():
    """按文件路径加载 `hooks/session-start.py`——文件名带连字符，不能直接 import。"""
    global _SESSION_START
    if _SESSION_START is not None:
        return _SESSION_START
    path = Path(__file__).resolve().parent.parent / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("memory_session_start", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"加载不了渲染器：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SESSION_START = mod
    return mod


def render_injected(root: Path, agent: str | None = None, project: Path | None = None) -> dict:
    """跑真正的注入渲染器，只取结果大小。

    **不能 subprocess 跑 `--dry`**：那条路会 `write_state` 并 `touch_access`，
    等于每开一次设置页就伪造一次「刚读过」，把 `archive.staleDays` 的陈旧判定毁掉。
    `load()` 本身是纯函数（`hooks/session-start.py:194`），所以直接调它。
    """
    if not agent:
        candidates = [default_agent(root)] + list_agents(root)
        agent = next((a for a in candidates if (root / "agents" / a).is_dir()), "default")
    cfg = (load_config(root) if load_config else {}) or {}
    kind = parse_frontmatter(project / "_meta.md").get("kind", "") if project else ""
    harness = agent.rsplit("-", 1)[-1] if "-" in agent else "unknown"
    warnings: list[str] = []
    mod = _session_start_module()
    output, injected = mod.load(root, agent, cfg, project, warnings, harness, kind)
    return {
        "agent": agent,
        "project": project.as_posix() if project else "",
        "kind": kind,
        "chars": len(output),
        "maxChars": int(cfg.get("inject", {}).get("maxChars", 12000)),
        "warnings": warnings,
        "injected": injected,
    }


def days_since(day: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(day)).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 项目册内部

def project_files(book: Path) -> list[str]:
    """项目册的子文件名。`_meta.md` / `index.md` 是骨架，不算。"""
    if not book.is_dir():
        return []
    return [p.name for p in sorted(book.glob("*.md")) if p.name not in {"_meta.md", "index.md"}]


def project_logs(book: Path) -> list[tuple[str, str, Path]]:
    """项目册里的日志，返回 [(日期, agent, 路径)]，按日期倒序。"""
    base = book / "log"
    if not base.is_dir():
        return []
    out = []
    for agent_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in agent_dir.glob("*.md"):
            out.append((f.stem, agent_dir.name, f))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def project_log_days(book: Path) -> int:
    """有几天日志。按日期去重——两个 agent 同一天各写一份算一天。"""
    return len({d for d, _, _ in project_logs(book)})


def project_latest_log(book: Path) -> str:
    logs = project_logs(book)
    return logs[0][0] if logs else ""


# 日志里的模板标题，不是任务名
SKIP_HEADS = {"做了什么", "结论", "待确认"}


def log_heads(path: Path) -> list[str]:
    """从日志里抽任务名。

    两种写法都要认：
      - `## 拆掉 domains（19:30）` + `### 做了什么`（10-26 起的新写法）
      - `## 做了什么` + `### 记忆系统重建（下午）`（三节模板套在合并日志上）
    所以二级和三级都抓，再排除模板标题。
    """
    try:
        text = read(path)
    except OSError:
        return []
    heads = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            title = s[3:].strip()
        elif s.startswith("### "):
            title = s[4:].strip()
        else:
            continue
        if title and title not in SKIP_HEADS:
            heads.append(title)
    return heads


def project_git_days(book: Path) -> dict[str, int]:
    """按日期数提交次数。用来补「有改动但没写日志」的空白。"""
    out = run_git(book, "log", "--format=%ad", "--date=format:%Y-%m-%d")
    counts = {}
    for line in out.splitlines():
        d = line.strip()
        if d:
            counts[d] = counts.get(d, 0) + 1
    return counts


# ---------------------------------------------------------------- focus / inbox

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def list_items(path: Path, date_last: bool) -> list[tuple[str, str]]:
    """从 markdown 列表里抽 (条目, 日期)。

    focus 的日期在行尾（`…｜2026-10-26`），inbox 的在行首（`2026-10-26 · …`）。
    一条里出现多个日期时按位置取对应的那个——用错位置会把正文里的日期当条目日期。
    占位行 `_（空）_` 不算条目。
    """
    if not path.is_file():
        return []
    out = []
    for line in read(path).splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        body = s[2:].strip()
        if not body or body.startswith("_"):
            continue
        dates = DATE_RE.findall(body)
        if not dates:
            continue
        out.append((body, dates[-1] if date_last else dates[0]))
    return out


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def column_width(cells: list[str], floor: int = 12, cap: int = 40) -> int:
    """列表的第一列宽度：按实际内容算，别写死。

    写死 28 的后果是长路径把整张表撑歪，而表格是给人扫的，一歪就看不清哪行
    对应哪个文件。上限 40 是为了防止一个超长路径把摘要挤到屏幕外。
    """
    longest = max((len(c) for c in cells), default=0)
    return min(max(longest + 2, floor), cap)


def count_recent_changes(text: str) -> int:
    """数「## 最近变更」下面有几条 `- `，到下一个 `## ` 为止。"""
    n = 0
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = "最近变更" in s
            continue
        if inside and s.startswith("- "):
            n += 1
    return n


# ---------------------------------------------------------------- 命令：status

def agent_log_spot(root: Path, agent: str) -> tuple[str, int, str]:
    """agent 的日志：最近一天、总天数、分布在哪几本册子。

    日志按「写进哪本册子」分散，总册的 `log/` 只是其中一处。2026-10-27 之前
    `status` 只看 `~/.memory/agents/<id>/log`（那个位置 10-26 起就不再写了），
    于是所有 agent 都显示「无日志」——和设置页概览同一个病，这里一起治。
    """
    stems: set[str] = set()
    places: list[str] = []

    def eat(base: Path, label: str) -> None:
        d = base / "log" / agent
        if not d.is_dir():
            return
        found = list(d.glob("*.md"))
        if not found:
            return
        stems.update(f.stem for f in found)
        places.append(f"{label} {len(found)} 天")

    eat(root, "总册")
    for item in read_workspaces(root):
        p = Path(item["path"]).expanduser()
        book = p / ".memory" if (p / ".memory").is_dir() else p
        if book.is_dir():
            eat(book, item.get("id") or p.name)

    if not stems:
        return "（无日志）", 0, ""
    return max(stems), len(stems), "、".join(places)


def cmd_status(root: Path) -> int:
    if not root.is_dir():
        print(f"记忆库不存在：{root}")
        print("用 `python memory.py init` 建一个。")
        return 1

    print(f"记忆库：{root}")

    books = list_playbooks(root)
    print(f"\nplaybooks {len(books)} 个（跨项目方法论）：")
    w1 = column_width([b["display"] for b in books])
    for b in books:
        flag = "" if all(b[k] for k in REQUIRED_FIELDS) else "  [字段不全]"
        print(f"  {b['display']:<{w1}} {b['summary'] or '（无 summary）'}{flag}")
    if not books:
        print("  （无）")

    topics = list_topics(root)
    print(f"\ntopics {len(topics)} 个（单文件主题）：")
    w2 = column_width([t["display"] for t in topics])
    for t in topics:
        flag = "" if all(t[k] for k in REQUIRED_FIELDS) else "  [字段不全]"
        print(f"  {t['display']:<{w2}} {t['summary'] or '（无 summary）'}{flag}")
    if not topics:
        print("  （无）")

    projects = [scan_project(w) for w in read_workspaces(root)]
    books = playbook_kinds(root)
    print(f"\n项目册 {len(projects)} 个（登记在 workspaces.json）：")
    for p in projects:
        if p["state"] == "missing":
            print(f"  {p['id']:<16} {p['path']}  [路径丢失]")
            continue
        idle = days_since(p["last_active"])
        tail = f"  最后动过 {p['last_active']}" if p["last_active"] else ""
        if idle is not None and idle >= PROJECT_IDLE_DAYS:
            tail += f"（{idle} 天没动）"
        print(f"  {p['id']:<16} {p['title'] or '（无标题）'}  {p['status'] or '（无状态）'}{tail}")
        print(f"  {'':<16} {p['path']}")
        print(f"  {'':<16} kind：{kind_label(p['kind'])}")
    if not projects:
        print("  （无）注册表还没建：跑 `init --project <路径>` 或手写 workspaces.json")

    print(f"\nkind 取值（= shared/playbooks/ 下的目录名）：")
    for name in sorted(set(KIND_NAMES) | set(books)):
        has = (root / "shared" / "playbooks" / name / "method.md").is_file()
        state = "有 method.md" if has else "（无 method.md，注入时跳过）"
        print(f"  {name:<16} {KIND_NAMES.get(name, '（自定义）'):<8} {state}")

    agents = list_agents(root)
    print(f"\nAgent {len(agents)} 个：")
    for name in agents:
        latest, days, where = agent_log_spot(root, name)
        tail = f"{days} 天" if days else ""
        print(f"  {name:<16} 最近日志 {latest}  {tail}".rstrip())
        if where:
            print(f"  {'':<16} 在 {where}")
    if not agents:
        print("  （无）")

    if (root / ".git").is_dir():
        last = run_git(root, "log", "-1", "--format=%h %ad %s", "--date=short")
        dirty = run_git(root, "status", "--porcelain")
        print(f"\nGit：{last or '（无提交）'}")
        if dirty:
            n = len([l for l in dirty.splitlines() if l.strip()])
            print(f"     有 {n} 个文件未提交（可能是你手改的）")
    return 0


def cmd_status_all(root: Path) -> int:
    """跨所有项目册汇总。和 `status` 的区别：那个看总册，这个看册子里面。"""
    print(f"总册：{root}")
    print(
        f"  playbooks {len(list_playbooks(root))} 个 · "
        f"topics {len(list_topics(root))} 个 · "
        f"agent {len(list_agents(root))} 个"
    )

    items = read_workspaces(root)
    print(f"\n项目册 {len(items)} 个：\n")
    if not items:
        print("  （注册表是空的，跑 `init --project <路径>`）")
        return 0

    for w in items:
        p = scan_project(w)
        if p["state"] != "ok":
            print(f"  {p['id']} · 路径丢失：{p['path']}")
            print("    修：`workspaces --fix " + f"{p['id']} <新路径>`\n")
            continue

        files = project_files(p["book"])
        logs = project_log_days(p["book"])
        latest = project_latest_log(p["book"])
        idle = days_since(p["last_active"])

        print(f"  {p['title'] or p['id']}（{p['id']}）  {p['status'] or '（无状态）'}")
        print(f"    kind {kind_label(p['kind'])}")
        print(f"    路径 {p['path']}")
        print(f"    子文件 {len(files)}：{'、'.join(files) if files else '（无）'}")
        print(f"    日志 {logs} 天{('，最近 ' + latest) if latest else '（还没写过）'}")
        tail = f"{p['last_active'] or '无记录'}"
        if idle is not None and idle >= PROJECT_IDLE_DAYS:
            tail += f"（{idle} 天没动）"
        print(f"    最后动过 {tail}")
        print()
    return 0


def cmd_workspaces(root: Path, argv: list[str]) -> int:
    """列登记的项目册。`--fix <id> <新路径>` 用来重绑丢失的路径。

    定位不靠这张表（靠 cwd 向上找 `_meta.md`），表只管「一共有哪些」。
    所以路径丢了不影响记忆可用，只让总表少一行——修不修都不急。
    """
    items = read_workspaces(root)

    if "--fix" in argv:
        return fix_workspace(root, items, argv)

    if not items:
        print("注册表是空的。")
        print(f"  {workspaces_path(root)}")
        print("用 `init --project <路径>` 建第一个项目册。")
        return 0

    broken = 0
    print(f"登记 {len(items)} 个项目册（{workspaces_path(root)}）：\n")
    for w in items:
        p = scan_project(w)
        if p["state"] != "ok":
            broken += 1
            print(f"  {p['id']:<16} 路径丢失")
            print(f"  {'':<16} {p['path']}")
            print(f"  {'':<16} 修：workspaces --fix {p['id']} <新路径>\n")
            continue

        logs = project_log_days(p["book"])
        idle = days_since(p["last_active"])
        idle_s = p["last_active"] or "无记录"
        if idle is not None and idle >= PROJECT_IDLE_DAYS:
            idle_s += f"（{idle} 天没动）"

        print(f"  {p['id']:<16} {p['status'] or '（无状态）':<6} {p['title'] or '（无标题）'}")
        print(f"  {'':<16} kind {kind_label(p['kind'])}")
        print(f"  {'':<16} {p['path']}")
        print(f"  {'':<16} 最后动过 {idle_s} · 日志 {logs} 天\n")

    if broken:
        print(f"{broken} 个路径丢失。项目挪了就跑 --fix，废弃了就手工从 workspaces.json 删。")
        return 1
    return 0


def fix_workspace(root: Path, items: list[dict], argv: list[str]) -> int:
    """按 id 重绑路径。id 的权威在项目册 `_meta.md`，不在注册表。"""
    i = argv.index("--fix")
    rest = [a for a in argv[i + 1:] if not a.startswith("--")]

    if len(rest) < 2:
        broken = [w for w in items if scan_project(w)["state"] != "ok"]
        if not broken:
            print("所有项目册路径都在，没有要修的。")
            return 0
        print("这些项目册的路径丢了：\n")
        for w in broken:
            p = scan_project(w)
            print(f"  {p['id']:<16} {p['path']}")
        print("\n用法：memory.py workspaces --fix <id> <新路径>")
        print("新路径指的是工作区根目录，不是 .memory 目录。")
        return 1

    pid, new_path = rest[0], Path(rest[1]).expanduser().resolve()
    book = new_path / ".memory"
    if not (book / "_meta.md").is_file():
        print(f"{book}\\_meta.md 不存在——新路径下没有项目册，不能重绑。")
        return 1

    real_id = parse_frontmatter(book / "_meta.md").get("id", "")
    if real_id and real_id != pid:
        print(f"提示：{book}\\_meta.md 里的 id 是 {real_id}，不是 {pid}。按 _meta.md 为准。")
        pid = real_id

    for item in items:
        if item.get("id") == pid:
            item["path"] = str(new_path)
            break
    else:
        items.append({"id": pid, "path": str(new_path)})

    write_workspaces(root, items)
    synced = sync_index_quiet(root)
    print(f"已重绑：{pid} → {new_path}")
    if synced:
        print(f"总索引已同步（{synced} 张表）。")
    return 0


# ---------------------------------------------------------------- 命令：checkup

def check_stale(root: Path) -> list[dict]:
    """归档候选：超过 staleDays 未被读取的文件。

    只报告，不动文件。没被读不等于没用——禁区、注意事项这类兜底信息
    本来就很少被单独打开，但违反了就出事。

    只扫「按需读取」的文件：playbooks / topics / user。
    注入清单里的文件（SOUL、index、profile 等）每次会话都读，戳记永远是新的，
    参与判断只会制造噪音。
    """
    stale_days = 180
    config_path = root / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(read(config_path))
            stale_days = int(cfg.get("archive", {}).get("staleDays", 180))
        except Exception:
            pass

    access = {}
    # 戳记按 agent 拆成了 access-<agent>.json，全读进来合并。
    # 只认 access-*.json：`access.json` 是 2026-10-26 之前的单文件版本，按字母序
    # 排在 access-* 之后，原来的 dict.update 会把新戳记覆盖成旧值。
    # 合并时取每个路径上最新的日期，不靠读取顺序——顺序无关才不会错。
    state_dir = root / ".state"
    if state_dir.is_dir():
        for sp in sorted(state_dir.glob("access-*.json")):
            try:
                files = json.loads(read(sp)).get("files", {})
            except Exception:
                continue
            for rel, stamp in files.items():
                if isinstance(stamp, str) and stamp > access.get(rel, ""):
                    access[rel] = stamp

    candidates = list_playbooks(root) + list_topics(root)
    candidates += [scan_md(f, root) for f in list_md(root / "shared" / "user")]

    result = []
    for item in candidates:
        stamp = access.get(item["rel"])
        if not stamp:
            result.append({"path": item["rel"], "days": None, "kind": "never"})
            continue
        days = days_since(stamp)
        if days is not None and days >= stale_days:
            result.append({"path": item["rel"], "days": days, "kind": "stale"})
    return result


LEVEL_NAMES = {1: "严重", 2: "该修", 3: "建议"}


def collect_checkup(root: Path, all_projects: bool = False) -> dict:
    """体检的数据层。返回结构化结果，`cmd_checkup` 和设置页共用这一份判断。

    `all_projects=True` 时「日志未提炼」扫所有登记项目册，不只当前 cwd 那个。
    """
    problems = []  # (严重度, 对象, 说明, 建议动作)

    # 0. 未提炼的日志——头号问题，排最前。按 agent 逐个扫，别只看默认那个
    scan_dirs = []  # (标签, 目录)
    if all_projects:
        for w in read_workspaces(root):
            p = scan_project(w)
            if p["state"] != "ok":
                continue
            base = p["book"] / "log"
            if not base.is_dir():
                continue
            label = p["title"] or p["id"]
            for d in sorted(x for x in base.iterdir() if x.is_dir()):
                scan_dirs.append((f"{label} · {d.name}", d))
    else:
        project = find_project_book(Path.cwd()) if find_project_book else None
        if project:
            base = project / "log"
            if base.is_dir():
                scan_dirs = [(d.name, d) for d in sorted(base.iterdir()) if d.is_dir()]
        else:
            scan_dirs = [(n, log_dir_for(None, root, n)) for n in list_agents(root)]

    if unextracted:
        for label, folder in scan_dirs:
            for day, n, st in unextracted(folder, days=3):
                why = "缺 `提炼：` 标记" if st == "missing" else "只写了「无」，没说理由"
                problems.append((
                    1, f"{label} · log/{day}",
                    f"{n} 条结论未提炼（{why}）",
                    "提炼进对应文件，再在日志末尾补 `提炼：文件名` 或 `提炼：无（理由）`",
                ))

    # 1. playbooks / topics 的元信息质量
    groups = [("playbooks", list_playbooks(root)), ("topics", list_topics(root))]
    for label, items in groups:
        whens = {}
        for item in items:
            f = item["display"]
            missing = [k for k in REQUIRED_FIELDS if not item.get(k)]
            if missing:
                problems.append((
                    1, label, f"{f} 缺字段：{'/'.join(missing)}",
                    "补齐元信息，否则索引和路由都不可靠",
                ))
                continue

            hit = [w for w in VAGUE_WORDS if w in item["summary"]]
            if hit:
                problems.append((
                    2, label, f"{f} 的 summary 有空话：{'、'.join(hit)}",
                    "换成具体名词，说清装的是什么",
                ))

            whens.setdefault(item["when"], []).append(f)

        for when, files in whens.items():
            if len(files) > 1:
                problems.append((
                    3, label, f"{'、'.join(files)} 的 when 完全相同",
                    "说不出区别就合并",
                ))

        if not items:
            problems.append((3, label, "没有任何文件", "有内容再建，别建空文件占位"))

    # 2. 项目册：路径、元信息、活跃度
    kinds = playbook_kinds(root)
    projects = [scan_project(w) for w in read_workspaces(root)]
    if not projects:
        problems.append((
            2, "workspaces",
            "注册表里没有项目册",
            "跑 `init --project <路径>` 建第一个，或手写 workspaces.json",
        ))
    for p in projects:
        label = p["id"] or p["path"].name
        if p["state"] == "missing":
            problems.append((
                1, f"项目册 {label}",
                f"路径丢失：{p['path']}\\.memory\\_meta.md 不存在",
                "项目挪了就跑 `workspaces --fix`，废弃了就手工从注册表删",
            ))
            continue

        # kind 单独查（下面三条），所以从「缺字段」里排掉，免得同一件事报两遍
        missing = [k for k in PROJECT_FIELDS if k != "kind" and not p["meta"].get(k)]
        if missing:
            problems.append((
                2, f"项目册 {label}",
                f"_meta.md 缺字段：{'/'.join(missing)}",
                "补 frontmatter，status 决定它算进行中还是搁置",
            ))

        # kind 单查一遍：它不在 PROJECT_FIELDS 的通用提示里说得清，错法有三种
        kind = p["meta"].get("kind", "")
        if not kind:
            problems.append((
                2, f"项目册 {label}",
                "_meta.md 没写 kind（不会注入任何 playbook）",
                f"填一个领域名，取值为 shared/playbooks/ 下的目录名：{'、'.join(sorted(kinds)) or '（一个都没有）'}",
            ))
        elif not KIND_RE.fullmatch(kind):
            problems.append((
                1, f"项目册 {label}",
                f"kind 格式不合法：{kind}",
                f"应为小写字母/数字/连字符、1-32 位；内置取值：{'、'.join(sorted(KIND_NAMES))}",
            ))
        elif not (root / "shared" / "playbooks" / kind / "method.md").is_file():
            problems.append((
                3, f"项目册 {label}",
                f"kind={kind} 没有对应的方法论文件（注入时会跳过）",
                f"建 shared/playbooks/{kind}/method.md（带 title/summary/when frontmatter），"
                f"或把 kind 改成已有 method.md 的：{'、'.join(sorted(kinds)) or '（一个都没有）'}",
            ))

        if not p["position"]:
            problems.append((2, f"项目册 {label}", "_meta.md 没有「## 定位」段", "补一句话定位"))
        elif len(p["position"]) > MAX_POSITION_CHARS:
            problems.append((
                3, f"项目册 {label}",
                f"定位 {len(p['position'])} 字，超过 {MAX_POSITION_CHARS}",
                "压短，定位不是描述",
            ))

        idle = days_since(p["last_active"])
        if idle is not None and idle >= PROJECT_IDLE_DAYS:
            problems.append((
                3, f"项目册 {label}",
                f"{idle} 天没动过",
                "确认还做不做；不做就把 status 改成「搁置」，别直接删",
            ))

        # 「最近变更」没有条数上限，而它是注入的项目册入口（≤2250 字符）——
        # 无限长等于每会话多付一笔预算，最后还是要靠调大 maxChars 止血。
        idx = p["book"] / "index.md"
        if idx.is_file():
            try:
                n_recent = count_recent_changes(read(idx))
            except OSError:
                n_recent = 0
            if n_recent > RECENT_CHANGES_MAX:
                problems.append((
                    3, f"项目册 {label}",
                    f"index.md「最近变更」{n_recent} 条，超过 {RECENT_CHANGES_MAX}",
                    "压到最近几条，更早的合并成一行「更早见 git log」",
                ))

    # 3. focus / inbox：两个兜底组件的腐烂检查
    focus_path = root / "shared" / "focus.md"
    if focus_path.is_file():
        fitems = list_items(focus_path, date_last=True)
        if len(fitems) > FOCUS_MAX_ITEMS:
            problems.append((
                2, "focus",
                f"{len(fitems)} 条，超过硬上限 {FOCUS_MAX_ITEMS}",
                "第 6 条进来必须先删一条。焦点不是待办清单",
            ))
        body_len = len(body_of(focus_path))
        if body_len > FOCUS_MAX_CHARS:
            problems.append((
                2, "focus",
                f"正文约 {body_len} 字，超过注入预算 {FOCUS_MAX_CHARS}",
                "整文件不会被注入，agent 看不到焦点。压成一行一句话",
            ))
        for body, day in fitems:
            gap = days_since(day)
            if gap is not None and gap > FOCUS_STALE_DAYS:
                problems.append((
                    2, "focus",
                    f"{gap} 天没进展：{clip(body, 24)}",
                    "更新状态或删掉。「进行中」不等于在推进",
                ))

        # 不变量：焦点里的项目，status 必须是「进行中」。
        # 条目是自由文本，只能拿 id / title 做子串匹配——匹配不上就不报。
        # 漏报的代价是这条检查偶尔失效；误报的代价是每次体检都有一条假问题，
        # 然后人就学会无视体检了。
        for p in projects:
            if p["state"] != "ok" or p["status"] in ("", "进行中"):
                continue
            needles = [n for n in (p["id"], p["title"]) if n]
            for body, _day in fitems:
                if any(n in body for n in needles):
                    problems.append((
                        2, "focus",
                        f"提到「{p['title'] or p['id']}」，但它的 status 是「{p['status']}」",
                        "焦点只放进行中的：要么把 status 改回来，要么从 focus 删掉",
                    ))
                    break

    inbox_path = root / "shared" / "inbox.md"
    if inbox_path.is_file():
        for body, day in list_items(inbox_path, date_last=False):
            gap = days_since(day)
            if gap is not None and gap > INBOX_STALE_DAYS:
                problems.append((
                    2, "inbox",
                    f"积压 {gap} 天：{clip(body, 24)}",
                    "四选一：进项目 / 进 playbooks / 进日志 / 删",
                ))

    # 4. 索引漂移：三张表各自和实际状态对
    index_path = root / "shared" / "index.md"
    if index_path.exists():
        text = read(index_path)
        for section, builder, known in index_targets(root):
            m = find_table(text, section)
            if not m:
                problems.append((
                    3, "shared",
                    f"index.md 里找不到「## {section}」下面的表",
                    "补一段标题和表头，或跑 `index --write` 生成",
                ))
                continue
            expected = merge_table(m.group(2), builder(root), known)
            if m.group(2).strip() != expected.strip():
                problems.append((
                    2, "shared",
                    f"总索引的{section}表和实际状态不一致",
                    "跑 `index --write` 同步",
                ))

    # 5. 配置自身的自洽性。`sync` / `schema` 曾经是零消费者字段——写了没人读，
    #    等于给了个假承诺：`sync.enabled: true` 会让人以为同步真的开着。
    config_path = root / "config.json"
    if config_path.is_file():
        cfg = {}
        try:
            cfg = json.loads(read(config_path))
        except Exception as exc:
            problems.append((
                1, "config.json", f"解析失败：{exc}",
                "修好 JSON。坏掉的配置会让所有阈值静默退回默认值",
            ))
        if cfg:
            want = CONFIG_DEFAULTS.get("schema")
            if cfg.get("schema") != want:
                problems.append((
                    2, "config.json",
                    f"schema={cfg.get('schema')!r}，代码期望 {want}",
                    "配置结构变了。对照 `memory.py init` 生成的默认值补字段，别手抄",
                ))
            if (cfg.get("sync") or {}).get("enabled"):
                problems.append((
                    2, "config.json",
                    "sync.enabled=true，但同步功能还没实现",
                    "改回 false。现在开它只是给未来的自己一个假承诺",
                ))
            unknown = sorted(set(cfg) - set(CONFIG_DEFAULTS))
            if unknown:
                problems.append((
                    3, "config.json",
                    f"有代码不认识的键：{'、'.join(unknown)}",
                    "删掉，或把它接进 CONFIG_DEFAULTS——不接就是下一个死字段",
                ))

    problems.sort(key=lambda x: x[0])

    # 归档候选：内容还有没有人用。和上面的「问题」分开——这些不是错，是信号
    stale = check_stale(root)

    todo = []
    seen = set()
    for _, obj, _desc, action in problems:
        key = (obj, action)
        if key in seen:
            continue
        seen.add(key)
        todo.append({"action": action, "object": obj})
        if len(todo) >= 3:
            break

    return {
        "root": root.as_posix(),
        "logScan": [label for label, _ in scan_dirs],
        "problems": [
            {"level": lv, "levelName": LEVEL_NAMES[lv], "object": obj, "desc": desc, "action": act}
            for lv, obj, desc, act in problems
        ],
        "topActions": todo,
        "stale": stale,
        "counts": {LEVEL_NAMES[lv]: len([p for p in problems if p[0] == lv]) for lv in (1, 2, 3)},
        # 不能叫 ok：/api 的响应信封已经占了 ok 字段，撞名会让前端把「没问题」当成请求失败
        "clean": not problems and not stale,
        "exitCode": 1 if any(p[0] == 1 for p in problems) else 0,
    }


def cmd_checkup(root: Path, all_projects: bool = False) -> int:
    """体检。输出问题清单，最后给「最该做的三件事」。文本和以前逐字一致。"""
    r = collect_checkup(root, all_projects)
    print(f"记忆库：{r['root']}")
    if r["logScan"]:
        print(f"日志扫描：{'、'.join(r['logScan'])}\n")
    else:
        print("日志扫描：（没找到任何 agent 日志目录）\n")

    if r["problems"]:
        for level in (1, 2, 3):
            group = [p for p in r["problems"] if p["level"] == level]
            if not group:
                continue
            print(f"【{LEVEL_NAMES[level]}】")
            for p in group:
                print(f"  {p['object']} · {p['desc']}")
                print(f"    → {p['action']}")
            print()

        print("最该做的三件事：")
        for i, item in enumerate(r["topActions"], 1):
            print(f"  {i}. {item['action']}（{item['object']}）")
        print()

    stale = r["stale"]
    if stale:
        old = [i for i in stale if i["kind"] == "stale"]
        never = [i for i in stale if i["kind"] == "never"]

        print("【归档候选】")
        for item in old:
            print(f"  {item['path']} · {item['days']} 天未读")
        if never:
            if len(never) <= 3:
                for item in never:
                    print(f"  {item['path']} · 从未记录过访问")
            else:
                # 一开始全是「从未」，逐个列只会变成噪音
                print(f"  {len(never)} 个文件还没有访问记录（戳记从启用那天开始攒）")
        print("    只是候选，不是结论。没被读不等于没用——禁区、注意事项本来就很少被单独打开")
        print("    决定归档就跑 `/memory-tidy`，或手动移到 archive/")
        print()

    if r["clean"]:
        print("体检通过，没有发现问题。")
        return 0

    return r["exitCode"]


# ---------------------------------------------------------------- 命令：digest
#
# 提炼提醒的判定逻辑只有这一份：WB 的 hooks/stop.py 与 DSH 的 Stop 都调
# `digest_check`，只是把结果打成各自 harness 认的外壳。以前它在 stop.py 里，
# WB 一旦冻结，DSH 侧就得抄一遍——抄一遍就是两个真相源。

# 提醒状态里最多保留多少条「会话 → 次数」记录
DIGEST_SESSION_RECORDS = 20


def _check_today_log(log_dir: Path) -> tuple[int, str] | None:
    """今天日志有结论但没交代提炼，返回 (结论条数, 状态)。"""
    p = log_dir / f"{date.today().isoformat()}.md"
    if not p.is_file():
        return None
    state, n = extraction_state(read_text(p))
    if n > 0 and state in {"missing", "none_bare"}:
        return n, state
    return None


def _load_digest(root: Path, agent: str) -> dict:
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


def digest_check(
    root: Path,
    agent: str,
    cwd: str | Path | None = None,
    turns: int = 0,
    session_id: str = "unknown",
) -> dict:
    """判断现在要不要提醒提炼记忆。命中时顺手写状态（冷却 + 本会话次数）。

    返回 `{should, text, kind, code}`：
    - `kind` 取 `extract`（今天日志没提炼）/ `stale`（库久未写）
    - `code` 是机器可读的原因：disabled / cooldown / session_used /
      no_write_time / few_turns / stale

    两道门控（冷却 360 分钟、每会话最多 1 次）在判定内部处理，
    调用方不需要自己数次数。
    """
    result = {"should": False, "text": "", "kind": "", "code": ""}

    if load_config is None:
        result["code"] = "no_state_module"
        return result

    config = load_config(root)
    digest = config.get("digest", {})
    if not digest.get("enabled", True):
        result["code"] = "disabled"
        return result

    after_min = int(digest.get("afterMinutes", 180))
    min_turns = int(digest.get("minTurns", 3))
    cooldown_min = int(digest.get("cooldownMinutes", 360))
    max_per_session = int(digest.get("maxPerSession", 1))

    now = datetime.now()
    state = _load_digest(root, agent)

    last_nudge = state.get("lastNudge", "")
    if last_nudge:
        try:
            if now - datetime.fromisoformat(last_nudge) < timedelta(minutes=cooldown_min):
                log(f"{agent} 冷却中，跳过")
                result["code"] = "cooldown"
                return result
        except ValueError:
            pass

    sessions = state.get("sessions", {})
    used = int(sessions.get(session_id, 0))
    if used >= max_per_session:
        result["code"] = "session_used"
        return result

    project = find_project_book(cwd) if cwd else None
    log_dir = log_dir_for(project, root, agent)

    # 第一优先：今天的日志有没有提炼
    pending = _check_today_log(log_dir)
    if pending:
        n, st = pending
        why = "缺 `提炼：` 标记" if st == "missing" else "只写了「无」，没说理由"
        result.update(
            should=True,
            kind="extract",
            text=(
                f"【提炼提醒】{agent} 今天的日志有 {n} 条结论，{why}。\n"
                "现在把结论提炼进项目册对应文件（`world.md` / `characters.md` / "
                "`playbooks/...`），然后在日志末尾补一行：\n"
                "`提炼：文件名` 或 `提炼：无（为什么不用提炼）`。\n"
                "只写「无」不算——那和没写分不出来。写完继续手头的事，不要开新任务。"
            ),
        )
    else:
        # 第二优先：记忆库多久没写
        lw = last_write_ms(root, project)
        if not lw:
            log("读不到写入时间，跳过")
            result["code"] = "no_write_time"
            return result
        stale_min = (now.timestamp() * 1000 - lw) / 60000
        if stale_min < after_min:
            log(f"记忆库 {stale_min:.0f} 分钟前刚写过，不提醒")
            return result
        if turns < min_turns:
            log(f"会话只有 {turns} 轮，不提醒")
            result["code"] = "few_turns"
            return result

        hours = stale_min / 60
        result.update(
            should=True,
            kind="stale",
            text=(
                f"【记忆提醒】距上次写入记忆库已 {hours:.1f} 小时。\n"
                "如果这次会话有值得留下的东西——决定、结论、踩过的坑、改过的设定——"
                "现在写进项目册。没有的话，在当天日志记一条「无新增」并说明原因，"
                "这样同一段时间内不会再提醒。写完继续手头的事，不要开新任务。"
            ),
        )

    sessions[session_id] = used + 1
    if len(sessions) > DIGEST_SESSION_RECORDS:
        for key in list(sessions)[: len(sessions) - DIGEST_SESSION_RECORDS]:
            sessions.pop(key, None)
    state["sessions"] = sessions
    state["lastNudge"] = now.isoformat(timespec="seconds")
    state["agent"] = agent
    write_state(root, state_name(agent, "digest.json"), state)
    log(f"{agent} 提醒已发出（{result['kind']}）")
    return result


def cli_agent(root: Path, argv: list[str]) -> str:
    """命令行里判断「我是谁」。

    顺序：`--agent` > 环境变量 `MEMORY_AGENT` > 由 `MEMORY_HARNESS` 推出的
    `<defaultAgent>-<harness>`。插件只设环境变量，不必知道 agent 全名。
    """
    given = flag(argv, "--agent")
    if given:
        return given

    env = os.environ.get("MEMORY_AGENT", "").strip()
    if env:
        return env

    harness = os.environ.get("MEMORY_HARNESS", "").strip()
    if harness:
        cfg = load_config(root) if load_config else {}
        base = str(cfg.get("defaultAgent") or "default")
        return f"{base}-{harness}"
    return ""


def cmd_digest(root: Path, argv: list[str]) -> int:
    """判定一次提炼提醒。命中时 stdout 只有一句提醒文案，退出码 2。

    DSH 的 Stop 用它：`--agent` 必填（插件从会话里读），`--turns` 是真实轮数。
    命中才输出，未命中输出为空、退出码 0——这样插件可以 `\"$(cmd)\"` 直接用。
    """
    agent = cli_agent(root, argv)
    if not agent:
        print("用法：digest --agent <id> [--cwd <路径>] [--turns N] [--session <id>] [--json]")
        return 1

    t = flag(argv, "--turns")
    r = digest_check(
        root,
        agent,
        cwd=flag(argv, "--cwd") or os.getcwd(),
        turns=int(t) if t and t.lstrip("-").isdigit() else 0,
        session_id=flag(argv, "--session") or "unknown",
    )

    if "--json" in argv:
        print(json.dumps(r, ensure_ascii=False))
        return 2 if r["should"] else 0
    if r["should"]:
        print(r["text"])
        return 2
    return 0


# ---------------------------------------------------------------- 命令：track
#
# 访问戳记只有一份实现（hooks/_state.py 的 touch_access，按天幂等、按 agent 分文件）。
# DSH 那边把 `read`/`ops_read_files` 的路径丢进来，这边转成库内相对路径再戳。

def cmd_track(root: Path, argv: list[str]) -> int:
    """把读过的记忆文件戳成今天。`--paths` 用 os.pathsep 分隔（Windows 是 ;）。

    不打印任何东西——它挂在 PostToolUse 上，输出会进上下文。
    """
    raw = flag(argv, "--paths") or ""
    if not raw.strip():
        return 0

    agent = cli_agent(root, argv)
    if not agent:
        agent = session_agent(root, flag(argv, "--session") or "") or ""

    rels = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        rel = relative_to_root(root, chunk)
        if rel:
            rels.append(rel)

    touch_access(root, rels, agent or None)
    return 0


# ---------------------------------------------------------------- 命令：skills
#
# DSH 读 `~/.dsh/skills/<name>/SKILL.md`（兼容平铺 `<name>.md`）。
# 插件目录更新后那份副本会过期，所以「装/查/换」都收在这个命令里，
# 而不是让用户手抄一份。Windows 无权限建符号链接时退回复制。

def dsh_skills_dir() -> Path:
    env = os.environ.get("DSH_HOME")
    base = Path(env) if env else Path.home() / ".dsh"
    return base / "skills"


def _same_skill(dst: Path, src: Path) -> bool:
    """两份 SKILL.md 内容是否一致。读不动就当不同（宁可多刷新一次）。"""
    try:
        return dst.read_bytes() == src.read_bytes()
    except OSError:
        return False


def cmd_skills(root: Path, argv: list[str]) -> int:
    """把插件自带的 skill 装进 `~/.dsh/skills/`。

    用法：
        skills            看现状（每个 skill 是已装 / 缺 / 过期）
        skills --install  装或刷新（默认建符号链接，失败退回复制）
        skills --mode copy|link --install  指定方式
    """
    src_dir = Path(__file__).resolve().parent.parent / "skills"
    if not src_dir.is_dir():
        print(f"找不到插件自带的 skills 目录：{src_dir}")
        return 1

    dst_root = dsh_skills_dir()
    mode = flag(argv, "--mode") or "auto"
    install = "--install" in argv

    if install:
        dst_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for skill_md in sorted(src_dir.glob("*/SKILL.md")):
        name = skill_md.parent.name
        dst = dst_root / name
        state = "missing"
        if dst.is_symlink():
            try:
                state = "ok" if dst.resolve() == skill_md.parent.resolve() else "different"
            except OSError:
                state = "different"
        elif dst.is_dir():
            # 复制模式装的那种：没有链接可比，就比主文件内容。
            # 有差异说明插件更新过、这份副本过期了。
            state = "ok" if _same_skill(dst / "SKILL.md", skill_md) else "stale"
        rows.append((name, state, dst, skill_md.parent))

    if install:
        for name, state, dst, src in rows:
            if state == "ok":
                continue
            if dst.exists() or dst.is_symlink():
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                else:
                    shutil.rmtree(dst)
            used = "link"
            if mode != "copy":
                try:
                    if sys.platform == "win32":
                        os.symlink(src, dst, target_is_directory=True)
                    else:
                        dst.symlink_to(src, target_is_directory=True)
                except OSError as exc:
                    if mode == "link":
                        print(f"{name}：建链接失败（{exc}）")
                        continue
                    log(f"建链接失败，改为复制：{exc}")
                    used = "copy"
            if used == "copy" or mode == "copy":
                shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"已装 {name}（{'符号链接' if used == 'link' else '复制'}）")

    print(f"\n目标目录：{dst_root}")
    w = max(len(n) for n, *_ in rows) if rows else 10
    marks = {"ok": "已装", "missing": "缺", "different": "已存在但不是同一份", "stale": "副本过期"}
    for name, state, dst, src in rows:
        if install and state != "ok":
            state = "ok" if (dst.exists() or dst.is_symlink()) else "missing"
        print(f"  {name:<{w}}  {marks[state]:<14} {dst}")
    if any(s != "ok" for _, s, _, _ in rows) and not install:
        print("\n装法：python core/memory.py skills --install")
    return 0


# ---------------------------------------------------------------- 命令：index

def build_workspaces_table(root: Path) -> str:
    lines = ["| 工作区 | 册子 | 装什么 |", "| --- | --- | --- |"]
    for w in read_workspaces(root):
        p = scan_project(w)
        shown = p["path"].as_posix()
        if p["state"] == "missing":
            lines.append(f"| `{shown}` | `.memory/` | **路径丢失** |")
            continue
        lines.append(f"| `{shown}` | `.memory/` | {p['position'] or p['title'] or '（待补）'} |")
    if len(lines) == 2:
        lines.append("| _（未登记）_ | | 跑 `init --project <路径>` 登记 |")
    return "\n".join(lines)


def build_playbooks_table(root: Path) -> str:
    lines = ["| 文件 | 装什么 | 何时读 |", "| --- | --- | --- |"]
    for b in list_playbooks(root):
        lines.append(
            f"| `{b['display']}` | {b['summary'] or '（待补）'} | {b['when'] or '（待补）'} |"
        )
    if len(lines) == 2:
        lines.append("| _（无）_ | | 有跨项目方法论再建 |")
    return "\n".join(lines)


def build_topics_table(root: Path) -> str:
    lines = ["| 文件 | 装什么 | 何时读 |", "| --- | --- | --- |"]
    for t in list_topics(root):
        lines.append(
            f"| `{t['display']}` | {t['summary'] or '（待补）'} | {t['when'] or '（待补）'} |"
        )
    if len(lines) == 2:
        lines.append("| _（无）_ | | 有单文件主题再建 |")
    return "\n".join(lines)


def index_targets(root: Path):
    """三张表的 (标题前缀, 生成函数, 由程序维护的第一列集合)。"""
    ws_known = {f"`{Path(w['path']).expanduser().as_posix()}`" for w in read_workspaces(root)}
    pb_known = {f"`{b['display']}`" for b in list_playbooks(root)}
    tp_known = {f"`{t['display']}`" for t in list_topics(root)}
    return [
        ("项目册", build_workspaces_table, ws_known),
        ("playbooks", build_playbooks_table, pb_known),
        ("topics", build_topics_table, tp_known),
    ]


def find_table(text: str, section: str):
    """找「## <标题>」之后的第一张表。标题后面的括号说明允许随便改。"""
    pattern = re.compile(r"(## " + re.escape(section) + r"[^\n]*\n+)((?:\|.*\n)+)")
    return pattern.search(text)


def norm_cell(cell: str) -> str:
    """归一化表格第一列，用来判断「这行是不是程序维护的」。

    路径有正反斜杠两种写法（注册表存正斜杠，Path 输出反斜杠），
    不归一化的话，旧表里的反斜杠行会被当成手写行保留，表格里出现重复。
    """
    return cell.strip().strip("`").replace("\\", "/")


def merge_table(current: str, new_table: str, known: set[str]) -> str:
    """用新表替换程序维护的行，用户手写的其他行原样追加在末尾。

    判断标准：第一列归一化后在 `known` 里的算「程序行」；其他行是主人自己加的，一律保留。
    """
    new_lines = new_table.splitlines()
    known_norm = {norm_cell(k) for k in known}
    extras = []
    for line in current.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells or norm_cell(cells[0]) in known_norm:
            continue
        if cells[0] in {"文件", "工作区", "分册"} or set(cells[0]) <= {"-", ":", " ", "_"}:
            continue  # 表头与分隔行
        if cells[0].startswith("_"):
            continue  # 占位行由 builder 生成，不当作手写行
        extras.append(s)
    return "\n".join(new_lines + extras)


def cmd_index(root: Path, do_write: bool) -> int:
    index_path = root / "shared" / "index.md"
    if not index_path.exists():
        print(f"找不到 {index_path}")
        return 1

    text = read(index_path)
    drifts = index_drift(root, text)
    missing = [s for s in INDEX_SECTIONS if not find_table(text, s)]

    for s in missing:
        print(f"跳过「## {s}」：index.md 里找不到这一段下面的表。")

    if not drifts:
        print("索引是最新的，没有漂移。")
        return 0

    print(f"索引有漂移（{len(drifts)} 张表）：\n")
    for section, m, merged in drifts:
        print(f"--- {section} · 现在 ---")
        print(m.group(2).rstrip())
        print(f"--- {section} · 应该是 ---")
        print(merged)
        print()

    if not do_write:
        print("（加 --write 才会真的写）")
        return 1

    write_index(root, text, drifts)
    print("已写入。")
    return 0


def index_drift(root: Path, text: str) -> list[tuple[str, re.Match, str]]:
    """三张表里对不上的部分。返回 [(标题, 匹配对象, 应该是什么)]。"""
    out = []
    for section, builder, known in index_targets(root):
        m = find_table(text, section)
        if not m:
            continue
        merged = merge_table(m.group(2), builder(root), known)
        if m.group(2).strip() != merged.strip():
            out.append((section, m, merged))
    return out


def write_index(root: Path, text: str, drifts: list) -> None:
    """从后往前替换，避免前面的替换把后面的偏移量改掉。"""
    for _, m, merged in sorted(drifts, key=lambda x: -x[1].start(2)):
        text = text[:m.start(2)] + merged + "\n" + text[m.end(2):]
    write(root / "shared" / "index.md", text)


def sync_index_quiet(root: Path) -> int:
    """静默同步索引，返回改动的表数。给 init --project 用。

    不静默的话，建完项目册下次 checkup 第一条就是「索引漂移」——
    那是我自己造成的，不该让主人去修。
    """
    index_path = root / "shared" / "index.md"
    if not index_path.is_file():
        return 0
    text = read(index_path)
    drifts = index_drift(root, text)
    if drifts:
        write_index(root, text, drifts)
    return len(drifts)


# ---------------------------------------------------------------- 命令：timeline

def pad(s: str, width: int) -> str:
    """按显示宽度补空格。中文占两格，len() 只算一格，直接用 format 会错位。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, width - w)


def cmd_timeline(root: Path, days: int = 30) -> int:
    """跨项目册的日志时间线。

    以日志为主——日志是结论层，读的是「做了什么」。git 提交只用来补
    「有改动但没写日志」的空白：两者都列会重复，19:40 既有日志「CLI 重写」
    又有提交「docs: 记 CLI 重写」，说的是同一件事。

    补丁的用处是科研册这种——建了册、有提交，但一条日志都没写，
    纯日志时间线里它会完全隐形。
    """
    items = read_workspaces(root)
    if not items:
        print("注册表是空的，没有时间线可看。")
        return 0

    logged = {}   # 日期 -> [(项目, [任务名])]
    commits = {}  # 日期 -> {项目: 次数}

    for w in items:
        p = scan_project(w)
        if p["state"] != "ok":
            continue
        label = p["title"] or p["id"]
        for date_s, _, path in project_logs(p["book"]):
            heads = log_heads(path)
            if heads:
                logged.setdefault(date_s, []).append((label, heads))
        for date_s, n in project_git_days(p["book"]).items():
            commits.setdefault(date_s, {})[label] = n

    dates = sorted(set(logged) | set(commits), reverse=True)
    # `--days 1` 意思是「只有今天」。用 < 而不是 <=，否则会把昨天也算进来
    dates = [d for d in dates if (g := days_since(d)) is not None and g < days]
    if not dates:
        print(f"最近 {days} 天没有记录。")
        return 0

    print(f"时间线（最近 {days} 天，{len(dates)} 个有记录的日子）：\n")
    for date_s in dates:
        gap = days_since(date_s)
        print(f"{date_s}  {'今天' if gap == 0 else f'{gap} 天前'}")

        shown = set()
        for label, heads in logged.get(date_s, []):
            shown.add(label)
            for i, h in enumerate(heads[:4]):
                print(f"  {pad(label, 16) if i == 0 else ' ' * 16}{h}")
            if len(heads) > 4:
                print(f"  {' ' * 16}…… 共 {len(heads)} 项")

        for label, n in sorted(commits.get(date_s, {}).items()):
            if label in shown:
                continue
            print(f"  {pad(label, 16)}（{n} 次改动，未写日志）")
        print()
    return 0


# ---------------------------------------------------------------- 命令：init

AGENT_TEMPLATES = {
    "SOUL.md": """---
title: SOUL
owner: user
created: {today}
updated: {today}
---

# SOUL.md - 我是谁

_（待写：这个 agent 的底色、准则、语气。改它要告诉主人。）_
""",
    "IDENTITY.md": """---
title: IDENTITY
owner: user
created: {today}
updated: {today}
---

# IDENTITY.md - 我是谁

- **Name:** {agent}
- **Harness:** {harness}
- **Vibe:** _待写_

---

_一个 agent 一间房。`shared/` 全员可见，这里只放只对这个 agent 成立的东西。_
""",
    # 2026-10-27 删掉 memory.md：标着 owner: auto 却没有任何生成器，注入排在
    # inject.order 第 8 位、每会话都进上下文，实际是两行有效内容的空模板。
    # 按判定树倒推它没有生态位——工具坑进 playbooks、工作方式进 SOUL/preferences、
    # 运行时状态进 focus。两份旧文件已移到 archive/agents-memory-md-2026-10-27/。
}


def cmd_init_agent(root: Path, agent: str, source: str | None = None) -> int:
    """建一个 agent 的私有目录。

    id 形如 <persona>-<harness>（moxie-wb / moxie-dsh）。不自动复制 SOUL：
    SessionStart 保持只读，建目录是显式动作。要复制就 `--from`。
    """
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", agent):
        print(f"agent id 不合法：{agent}（只允许小写字母、数字、连字符）")
        return 1

    folder = root / "agents" / agent
    if folder.exists():
        print(f"{folder} 已存在，不覆盖。")
        return 1

    src = None
    if source:
        src = root / "agents" / source
        if not src.is_dir():
            print(f"源 agent 不存在：{src}")
            return 1

    today = date.today().isoformat()
    harness = agent.rsplit("-", 1)[-1] if "-" in agent else "unknown"

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "log").mkdir(exist_ok=True)

    for name, tpl in AGENT_TEMPLATES.items():
        if src and (src / name).is_file():
            text = read(src / name)
            text = text.rstrip("\n") + (
                f"\n\n---\n\n_从 `agents/{source}/` 复制于 {today}。"
                "之后两边独立演化，不自动同步。_\n"
            )
        else:
            text = tpl.format(today=today, agent=agent, harness=harness)
        (folder / name).write_text(text, encoding="utf-8", newline="\n")

    print(f"已创建 agent：{folder}")
    if src:
        print(f"SOUL / IDENTITY 复制自 {source}，末尾标了来源日期。")
    else:
        print("SOUL 是空模板，下一步写它——人格是主人的地盘。")
    return 0


PROJECT_META = """---
id: {id}
kind: {kind}
workspace: {path}
title: {title}
status: 进行中
created: {today}
updated: {today}
---

# {title} · 项目册

## 定位

_待补：一句话说清这个项目是什么。超过 {max_pos} 字 checkup 会提示。_

## 注意事项

**禁区**

- 不贴代码 / 正文原文。这里只放「为什么这么定」和结论
- 不记流水账。进度进 `log/`，结论进 `core.md`

**前置动作**

- 动这个项目前，先读 `index.md` 的「待确认」，别把悬而未决的当成已定

**读取提示**

- 结论以本册为准；过程与细节看工作区根目录的交接文档（如果有）
"""

PROJECT_INDEX = """---
title: {title} · 入口
owner: ai
updated: {today}
---

# {title}

**当前状态**：刚建册，还没有内容。

## 子文件

| 文件 | 装什么 | 何时读 |
| --- | --- | --- |
| `core.md` | 核心事实、已定的决定 | 开始干活之前 |
| `log/` | 按 agent 分的每日结论日志 | 回看进度、补提炼 |

## 待办

- [ ] 补 `_meta.md` 的「## 定位」
- [ ] 把已知的事实写进 `core.md`

## 待确认

_（无）_

## 最近变更

- {today} 建册
"""

PROJECT_CORE = """---
title: 核心事实
owner: ai
created: {today}
updated: {today}
---

# 核心事实

> 只放这个项目的事实和已定的决定。过程在 `log/`，方法论在 `~/.memory/shared/playbooks/`。

## 基本盘

_待补_

## 已定的决定

_（每条带理由。想推翻先看理由，再给新证据。）_
"""


# ------------------------------------------------------------- 建册的四道守卫


# 系统目录名（小写比较）。只列一定会长歪的地方，漏掉的交给主人判断
SYSTEM_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "system32", "syswow64", "usr", "etc", "bin", "sbin", "var", "opt",
    "library", "system", "applications",
}


def excluded_reason(target: Path, root: Path) -> str | None:
    """技术性排除：这些位置不建册，返回原因；可以建返回 None。

    排除是硬的、可枚举的，不判断「这个目录值不值得有长期记忆」——那个判断
    要意图，AI 只看得见文件。想建就明说，别让默认行为替你决定。
    """
    t = target.resolve()
    r = root.resolve()
    if t == Path.home().resolve():
        return "这是 home 目录，东西会越堆越杂"
    if t.parent == t:
        return "这是盘根"
    if t == r:
        return "这是记忆库自己"
    if r in t.parents:
        return "记忆库内部的目录不需要再建册"
    if t.name.lower() in SYSTEM_DIRS or any(p.lower() in SYSTEM_DIRS for p in t.parts[1:3]):
        return "系统目录"
    # 已经在某本项目册内部：往上找 .memory/_meta.md
    for parent in t.parents:
        if (parent / ".memory" / "_meta.md").is_file():
            return f"已经在 {parent.as_posix()} 的项目册内部"
    return None


def ensure_gitignore(target: Path) -> str:
    """工作区是 git 仓库时，把 `.memory/` 加进 `.gitignore`。

    项目册自己也是 git 仓库；不 ignore 的话，一提交就是「嵌套仓库」，主仓库只记一个
    灰格子（gitlink），别人拿到的是空目录。返回 added / exists / skipped。
    """
    if not (target / ".git").exists() and not run_git(target, "rev-parse", "--git-dir"):
        return "skipped"
    gi = target / ".gitignore"
    if not gi.is_file():
        gi.write_text(".memory/\n", encoding="utf-8")
        return "added"
    text = gi.read_text(encoding="utf-8", errors="replace")
    lines = {line.strip() for line in text.splitlines()}
    if {".memory/", ".memory", "/.memory"} & lines:
        return "exists"
    sep = "" if (not text or text.endswith("\n")) else "\n"
    gi.write_text(text + sep + ".memory/\n", encoding="utf-8")
    return "added"


def write_init_log(book: Path, agent: str, pid: str, title: str, kind: str) -> Path:
    """建册也留一行日志：日志记「那天做了什么」，建册算一件事。

    顺手把「提炼：无（建册）」写上，免得当天结尾的提炼检查把这一天判成漏提炼。
    """
    today = date.today().isoformat()
    d = book / "log" / agent
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{today}.md"
    head = f"# {today}\n\n" if not path.exists() else ""
    body = (
        "## 建册\n\n"
        f"- 建册：{title}（{pid}）" + (f"，kind `{kind}`" if kind else "，kind 未定") + "\n"
        "- 定位：待补（`_meta.md` 的「## 定位」，≤40 字）\n"
        "- 已知事实：待补（`core.md`）\n\n"
        "提炼：无（建册）\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(head + body)
    return path


def verify_injection(target: Path, title: str) -> tuple[bool, str]:
    """建完真跑一次渲染器，确认注入里能看到这本册子。

    用 `--dry`。注意它仍会写 `.state` 的访问戳记——所以只在建册时跑一次，
    不放进循环里重复调。
    """
    script = Path(__file__).resolve().parent.parent / "hooks" / "session-start.py"
    if not script.is_file():
        return False, "找不到 hooks/session-start.py"
    env = {**os.environ, "MEMORY_HARNESS": "dsh", "PYTHONIOENCODING": "utf-8"}
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--dry", "--cwd", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
    except Exception as e:  # 超时、找不到 python，都不该让建册失败
        return False, f"跑不起来：{e}"
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip().splitlines()
        return False, (tail[-1] if tail else f"退出码 {out.returncode}")[:200]
    if title not in out.stdout:
        return False, "注入里没看到册子标题"
    return True, ""


def cmd_init_project(
    root: Path,
    target: Path,
    pid: str | None,
    title: str | None,
    kind: str | None = None,
) -> int:
    """建一个项目册骨架，并登记进 workspaces.json。

    项目册放 `<workspace>/.memory/`：跟项目走、就近可读、删目录时记忆一起删
    （比留一堆「僵尸记忆」好）。目录名用 `.memory` 是主人定的——不污染项目根。

    `kind` 决定注入哪份 `playbooks/<kind>/method.md`。**故意不设默认值**：
    猜一个「engineering」会让「忘了传」变成静默错误，不如留空让 checkup 报出来。
    """
    target = target.expanduser().resolve()
    book = target / ".memory"

    reason = excluded_reason(target, root)
    if reason:
        print(f"这块不建册：{reason}")
        print("  要建就换一个目录；排除表是给默认行为设的边界，不是禁令。")
        return 1

    if not target.is_dir():
        print(f"目录不存在：{target}")
        print("  建册不顺手建工作区。先 mkdir，再来。")
        return 1

    if (book / "_meta.md").exists():
        print(f"{book} 已经有 _meta.md，不覆盖。")
        print("  想补字段：设置页「人格」页，或直接改 _meta.md。")
        return 1

    # 半建：目录在、_meta 不在。别静默往上盖，先让人决定
    if book.is_dir() and any(book.iterdir()):
        names = "、".join(sorted(p.name for p in book.iterdir())[:6])
        print(f"{book} 存在但没有 _meta.md —— 这本册子不完整。")
        print(f"  里面已有：{names}")
        print("  要么手工补 _meta.md（frontmatter 至少 id / title / status），要么删掉 .memory/ 重来。")
        return 1

    pid = (pid or slugify(target.name)).strip()
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", pid):
        print(f"id 不合法：{pid}（只允许小写字母、数字、连字符，用 --id 指定）")
        return 1

    kind = (kind or "").strip()
    if kind and not KIND_RE.fullmatch(kind):
        legal = "、".join(sorted(KIND_NAMES))
        print(f"kind 不合法：{kind}")
        print(f"  只允许小写字母、数字、连字符，1-32 位；内置取值：{legal}")
        return 1

    title = (title or target.name).strip()
    today = date.today().isoformat()

    book.mkdir(parents=True, exist_ok=True)
    write(book / "_meta.md", PROJECT_META.format(
        id=pid, kind=kind, path=target.as_posix(), title=title,
        today=today, max_pos=MAX_POSITION_CHARS,
    ))
    write(book / "index.md", PROJECT_INDEX.format(title=title, today=today))
    write(book / "core.md", PROJECT_CORE.format(today=today))

    agent = default_agent(root)
    log_dir = book / "log" / agent
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / ".gitkeep").write_text("", encoding="utf-8")
    log_path = write_init_log(book, agent, pid, title, kind)

    # 项目册独立 git：它跟项目走，不能混进工作区的仓库
    run_git(book, "init", "-q")
    run_git(book, "add", "-A")
    run_git(
        book, "-c", "user.name=moxie", "-c", "user.email=moxie@local",
        "commit", "-q", "-m", f"init: {title} 项目册骨架",
    )
    # 主仓库必须 ignore 它，否则只留一个 gitlink 灰格子
    gi = ensure_gitignore(target)

    # 登记进注册表。已有同 path 的就地更新，不重复添加
    items = read_workspaces(root)
    for item in items:
        if Path(item["path"]).expanduser().resolve() == target:
            item["id"] = pid
            break
    else:
        items.append({"id": pid, "path": str(target)})
    write_workspaces(root, items)

    synced = sync_index_quiet(root)

    print(f"已建项目册：{book}")
    print(f"  id: {pid}    title: {title}")
    print(f"  kind：{kind_label(kind)}")
    if not kind:
        print("  没写 kind 就不会注入任何 playbook。补法：编辑 _meta.md 的 kind 一行，"
              "取值看 `python memory.py status` 的 kind 列表。")
    elif kind not in playbook_kinds(root):
        print(f"  注意：shared/playbooks/{kind}/ 还不存在，注入时会跳过。")
        print(f"  建法：写 shared/playbooks/{kind}/method.md（至少带 title/summary/when frontmatter）。")
    print(f"  建册日志：log/{agent}/{log_path.name}")
    if gi == "added":
        print("  已把 .memory/ 写进工作区的 .gitignore")
    elif gi == "exists":
        print("  .gitignore 里本来就有 .memory/")
    else:
        print("  工作区还不是 git 仓库，没有 .gitignore 要改")
    print(f"  已登记进 {workspaces_path(root)}")
    if synced:
        print(f"  总索引已同步（{synced} 张表）")
    print("下一步：写 _meta.md 的「## 定位」，把已知事实填进 core.md。")

    ok, detail = verify_injection(target, title)
    if ok:
        print("自动验证：注入渲染通过，标题进得去")
    else:
        print(f"自动验证：没过 —— {detail}")
        print("  手工验：python hooks/session-start.py --dry --cwd <这个目录>")
    return 0


def cmd_init(target: Path) -> int:
    if target.exists() and any(target.iterdir()):
        print(f"{target} 已存在且非空，不覆盖。")
        return 1

    # 2026-10-26：不再建 shared/domains。可枚举的东西是 playbooks + topics。
    dirs = [
        "shared/user", "shared/playbooks", "shared/topics",
        "shared/decisions", "shared/raw",
        "agents", "archive",
    ]
    for d in dirs:
        (target / d).mkdir(parents=True, exist_ok=True)

    # 默认配置只有一份（CONFIG_DEFAULTS）；设置页的缺失键回退也从那里拿
    (target / "config.json").write_text(
        json.dumps(CONFIG_DEFAULTS, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    write_workspaces(target, [])

    write(target / "shared" / "index.md", """---
title: 总册路由
owner: auto
updated: {today}
---

# 总册路由

> 三张表由 `index --write` 维护，手写的行会保留。

## 项目册

{ws}

## playbooks

{pb}

## topics

{tp}

## user

- `user/profile.md` — 主人档案
- `user/preferences.md` — 稳定偏好
""".format(
        today=date.today().isoformat(),
        ws=build_workspaces_table(target),
        pb=build_playbooks_table(target),
        tp=build_topics_table(target),
    ))

    print(f"已创建空库：{target}")
    print("下一步：写 README.md（协议），跑 `init --agent <id>` 建第一个人格目录。")
    return 0


# ---------------------------------------------------------------- 入口

def flag(argv: list[str], name: str) -> str | None:
    """取 `--x value` 形式的值。"""
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv):
        return None
    return argv[i + 1]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0

    cmd = argv[1]
    root = find_root()

    if cmd == "status":
        return cmd_status_all(root) if "--all" in argv else cmd_status(root)
    if cmd == "checkup":
        return cmd_checkup(root, "--all" in argv)
    if cmd == "index":
        return cmd_index(root, "--write" in argv)
    if cmd == "workspaces":
        return cmd_workspaces(root, argv)
    if cmd == "timeline":
        d = flag(argv, "--days")
        return cmd_timeline(root, int(d) if d and d.isdigit() else 30)
    if cmd == "digest":
        return cmd_digest(root, argv)
    if cmd == "track":
        return cmd_track(root, argv)
    if cmd == "skills":
        return cmd_skills(root, argv)
    if cmd == "init":
        if "--agent" in argv:
            agent = flag(argv, "--agent")
            if not agent:
                print("用法：init --agent <id> [--from <id>]")
                return 1
            return cmd_init_agent(root, agent, flag(argv, "--from"))
        if "--project" in argv:
            target = flag(argv, "--project")
            if not target:
                print("用法：init --project <路径> [--id <id>] [--title <标题>] [--kind <领域>]")
                return 1
            return cmd_init_project(
                root, Path(target), flag(argv, "--id"), flag(argv, "--title"),
                flag(argv, "--kind"),
            )
        # `init --help` 曾经把 "--help" 当路径，在插件目录里建了一个叫 --help 的库
        if len(argv) > 2 and str(argv[2]).startswith("-"):
            print(f"不认识这个参数：{argv[2]}\n")
            print(__doc__)
            return 1
        target = Path(argv[2]).expanduser().resolve() if len(argv) > 2 else root
        return cmd_init(target)

    print(f"未知命令：{cmd}\n")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
