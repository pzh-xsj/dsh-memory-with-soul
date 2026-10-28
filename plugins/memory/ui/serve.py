#!/usr/bin/env python3
"""记忆库设置页的本地服务。

两个职责：

1. 发静态文件。`file://` 打开时 File System Access API 不可用（浏览器只在
   安全上下文里给 `showDirectoryPicker`），所以设置页必须走 http://localhost。
2. 提供 `/api/*`：页面要显示的事实（体检、注入预算、项目册、目录树）从
   `core/memory.py` 现算，不在 JS 里重写第二份判断。**这条是 2026-10-27 修
   UI 时的唯一硬约束**——上一版把体检逻辑抄进前端，架构一改（拆掉
   `shared/domains`）就全坏了，页面还在报一个不存在的「分册」。

写入只有这五条，都是「结构化、白名单、先备份」：

  POST /api/project-meta   改项目册 `_meta.md` 的 status / kind / title /
                           「## 定位」/「## 注意事项」三块
  POST /api/config         改 `config.json` 的白名单键，其余键原样保留；
                           带 `order` 时改注入顺序（只调 priority，不许增删条目）
  POST /api/checkup        **执行修复**：`{"kinds": [...], "dry": bool}`，
                           只做体检里标了 `fix` 的四类机械问题，不收命令字符串
  POST /api/playbook       新建领域 = `shared/playbooks/<kind>/method.md`。
                           参数只有四个字段，正文留空给人写；不删不改名
  POST /api/persona /file  整份覆盖 SOUL.md 与任意 `.md`（带 mtime 冲突检测）

自由文本（SOUL、任意 md）的编辑仍然走浏览器的目录授权，服务端不碰——给前端
一个通用写接口就等于给了它改任何文件的权力，收益不值这个风险。

用法：

  python ui/serve.py                 # 8765 起服务，自动开浏览器
  python ui/serve.py --port 9000     # 换端口（占用了会自动往后挪）
  python ui/serve.py --no-browser    # 不开浏览器
  python ui/serve.py --self-test     # 自检：渲染器、API 契约、写入幂等

自检不需要真的开浏览器，它在临时端口上起一个真服务，再用 urllib 打自己。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "core"))

import memory as mem  # noqa: E402

DEFAULT_PORT = 8765
PORT_TRIES = 20
MAX_FILE_BYTES = 2 * 1024 * 1024
BACKUP_SUBDIR = Path(".state") / "ui-backups"

# 服务端允许改的配置键：(类型, 最小值, 最大值)。不在表里的一律拒绝，
# 免得前端一个手滑把 inject.order 清空——那等于人格注入消失。
CONFIG_RULES = {
    ("inject", "maxChars"): ("int", 1000, 200000),
    ("digest", "enabled"): ("bool", None, None),
    ("digest", "afterMinutes"): ("int", 30, 100000),
    ("digest", "minTurns"): ("int", 1, 100),
    ("digest", "cooldownMinutes"): ("int", 30, 100000),
    ("digest", "maxPerSession"): ("int", 1, 100),
    ("archive", "staleDays"): ("int", 30, 36500),
    ("sync", "enabled"): ("bool", None, None),
    ("sync", "remote"): ("str", None, None),
}


# ---------------------------------------------------------------- 路径与备份

def safe_path(root: Path, rel: str) -> Path:
    """把相对路径钉在记忆库内。`../` 和绝对路径一律拒绝。"""
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        raise ValueError("路径不合法")
    p = (root / rel.replace("\\", "/")).resolve()
    if p != root and root not in p.parents:
        raise ValueError("路径越界")
    return p


def project_book(ws: str) -> Path:
    """工作区根 → `.memory` 目录。项目册可能在任何盘上，不要求在记忆库内。"""
    book = (Path(ws).expanduser() / ".memory").resolve()
    if not (book / "_meta.md").is_file():
        raise FileNotFoundError(f"{book} 下没有 _meta.md")
    return book


def backup_dir_for(path: Path, root: Path) -> Path:
    """备份放 `.state/ui-backups`。

    放 `.state` 是因为它已经在 .gitignore 里——备份如果进了 git，
    每次改 _meta.md 都多一个文件要提交，最后只会把 git 用废。
    """
    base = root if root in path.parents else path.parent
    return base / BACKUP_SUBDIR


def backup(path: Path, root: Path) -> Path | None:
    if not path.is_file():
        return None
    folder = backup_dir_for(path, root)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 拍平成文件名：备份目录里不该按原路径再建一遍目录树
    flat = str(path).replace("\\", "/").replace(":", "").replace("/", "__")
    dest = folder / f"{flat}.{stamp}.bak"
    shutil.copy2(path, dest)
    return dest


# ---------------------------------------------------------------- 数据视图

def pick_agent(root: Path, cfg: dict, wanted: str | None = None) -> tuple[str, bool]:
    """挑一个真实存在的身份来预演注入。

    返回 (身份, 是否回退过)。`config.defaultAgent` 可能指向已经删掉的目录
    （本机就写着 `moxie`，2026-10-26 已删），不校验的话预演会显示一个
    「缺 SOUL / 没有日志」的空身份，看的人会误以为记忆库坏了。
    """
    names = mem.list_agents(root)
    if wanted and wanted in names:
        return wanted, False
    for cand in (cfg.get("defaultAgent"), mem.default_agent(root)):
        if cand and cand in names:
            return cand, bool(wanted and wanted != cand)
    if names:
        return names[0], bool(wanted and wanted != names[0])
    return (wanted or "moxie"), False


def agent_logs(root: Path, name: str, books: list[dict]) -> dict:
    """日志住在各项目册的 `log/<agent>/`，总册 `agents/<agent>/log` 是老位置，也认。

    只看老位置会读出一份过期数据（本机 moxie-wb 显示 2026-10-25、1 天，
    实际项目册里已有 2 天），所以两边合起来算天数。
    """
    seen: dict[str, str] = {}
    dirs: list[tuple[Path, str]] = [(root / "agents" / name / "log", "总册")]
    for b in books:
        if b.get("state") == "ok":
            dirs.append((Path(b["book"]) / "log" / name, b["id"]))
    for d, src in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            day = getattr(mem, "log_day", None)
            seen.setdefault((day(f.name) if day else "") or f.stem, src)
    days = sorted(seen)
    return {
        "latestLog": days[-1] if days else "",
        "logDays": len(days),
        "logWhere": seen[days[-1]] if days else "",
    }


def project_from_cwd(start: Path) -> Path | None:
    """从某个目录往上找 `.memory/_meta.md`，和渲染器、CLI 的判定保持一致。"""
    for d in [start, *start.parents]:
        book = d / ".memory"
        if (book / "_meta.md").is_file():
            return book
    return None


def view_overview(root: Path, agent: str | None = None, cwd: str | None = None) -> dict:
    cfg = (mem.load_config(root) if mem.load_config else {}) or {}
    names = mem.list_agents(root)
    agent, fell_back = pick_agent(root, cfg, agent)
    books = mem.project_books(root)
    agents = [dict(id=name, **agent_logs(root, name, books)) for name in names]

    # 预演尽量贴近一次真会话：按 cwd 找项目册，kind 才不是空的
    try:
        cwd_path = Path(cwd).expanduser() if cwd else Path.cwd()
        cwd_path = cwd_path.resolve()
    except OSError:
        cwd_path = Path.cwd()
    project = project_from_cwd(cwd_path)

    focus_path = root / "shared" / "focus.md"
    inbox_path = root / "shared" / "inbox.md"
    focus_items = ([{"body": b, "date": d} for b, d in mem.list_items(focus_path, date_last=True)]
                   if focus_path.is_file() else [])
    inbox_items = ([{"body": b, "date": d} for b, d in mem.list_items(inbox_path, date_last=False)]
                   if inbox_path.is_file() else [])

    try:
        injected = mem.render_injected(root, agent, project=project)
    except Exception as exc:  # 渲染器坏了不能让整页白屏
        injected = {"error": f"{type(exc).__name__}: {exc}"}

    # 角色卡：把「每个 agent 读哪张卡」一次算好，前端不再自己解析
    agent_roles = {}
    if getattr(mem, "resolve_role", None) is not None:
        for name in names:
            try:
                r, rsrc = mem.resolve_role(root, name)
            except Exception:
                r, rsrc = "", "none"
            agent_roles[name] = {"role": r, "source": rsrc}
    mine = agent_roles.get(agent) or {"role": "", "source": "none"}
    agent_role, agent_role_src = mine["role"], mine["source"]
    roles = mem.list_roles(root, cfg) if getattr(mem, "list_roles", None) else []
    default_role = mem.default_role(root, cfg) if getattr(mem, "default_role", None) else ""

    dirty = mem.run_git(root, "status", "--porcelain")
    return {
        "root": root.as_posix(),
        "config": cfg,
        "agent": agent,
        "agentNames": names,
        "agentFellBack": fell_back,
        "cwd": cwd_path.as_posix(),
        "projectBook": project.as_posix() if project else "",
        "agents": agents,
        "injected": injected,
        "projects": books,
        "playbooks": [
            {"display": b["display"], "summary": b["summary"], "when": b["when"]}
            for b in mem.list_playbooks(root)
        ],
        "topics": [
            {"display": t["display"], "summary": t["summary"], "when": t["when"]}
            for t in mem.list_topics(root)
        ],
        "focus": focus_items,
        "focusChars": len(mem.body_of(focus_path)) if focus_path.is_file() else 0,
        "inbox": inbox_items,
        "git": {
            "last": mem.run_git(root, "log", "-1", "--format=%h %ad %s", "--date=short"),
            "dirty": len([l for l in dirty.splitlines() if l.strip()]),
        },
        "kinds": sorted(mem.playbook_kinds(root)),
        "statusValues": list(mem.EDITABLE_STATUS),
        "roles": roles,
        "agentRole": agent_role,
        "agentRoleSource": agent_role_src,
        "agentRoles": agent_roles,
        "defaultRole": default_role,
    }


def view_tree(root: Path) -> dict:
    files = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if any(part in {".git", ".state", "node_modules"} for part in rel.split("/")):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
    return {"root": root.as_posix(), "files": files}


def view_file(root: Path, rel: str) -> dict:
    p = safe_path(root, rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"文件太大（{p.stat().st_size} 字节），用编辑器打开")
    return {"path": rel, "text": mem.read(p), "mtime": p.stat().st_mtime}


def view_config(root: Path) -> dict:
    cfg = (mem.load_config(root) if mem.load_config else {}) or {}
    return {
        "config": cfg,
        "defaults": mem.CONFIG_DEFAULTS,
        "path": (root / "config.json").as_posix(),
        "exists": (root / "config.json").is_file(),
        "editable": [[a, b] for a, b in CONFIG_RULES],
    }


def _coerce(kind: str, value):
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("要是 true/false")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("要是数字")
        return int(value)
    if not isinstance(value, str):
        raise ValueError("要是字符串")
    return value.strip()


def apply_config_patch(root: Path, patch: dict) -> dict:
    """只改白名单键。类型/范围不对直接抛，不做「悄悄修正」。"""
    cfg = (mem.load_config(root) if mem.load_config else {}) or {}
    if not cfg:
        cfg = json.loads(json.dumps(mem.CONFIG_DEFAULTS, ensure_ascii=False))
    changed = []
    for (section, key), (kind, low, high) in CONFIG_RULES.items():
        if section not in patch or not isinstance(patch[section], dict):
            continue
        if key not in patch[section]:
            continue
        raw = patch[section][key]
        value = _coerce(kind, raw)
        if kind == "int" and low is not None and (value < low or value > high):
            raise ValueError(f"{section}.{key} 要在 {low}–{high} 之间")
        if kind == "str" and len(value) > 500:
            raise ValueError(f"{section}.{key} 太长")
        block = cfg.setdefault(section, {})
        if block.get(key) != value:
            block[key] = value
            changed.append(f"{section}.{key}")

    if changed:
        backup(root / "config.json", root)
        mem.write(root / "config.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return {"changed": changed, "config": cfg}


# ---------------------------------------------------------------- HTTP

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


# 2026-10-27 起人格只有 SOUL.md 一个文件：身份/称呼并进 SOUL，旧 IDENTITY.md
# 的合并工具已在 0.6.6 删除。留着旧名会让 UI 以为还能写它。
PERSONA_FILES = ("SOUL.md",)


def role_of_agent(root: Path, agent: str) -> tuple[str, str]:
    """agent → (角色, 来源)。没配角色就返回 ("", "none")。"""
    if not AGENT_ID_RE.match(agent or ""):
        raise ValueError("agent 名不合法")
    resolve_role = getattr(mem, "resolve_role", None)
    if resolve_role is None:
        raise ValueError("这个版本没有角色卡支持")
    return resolve_role(root, agent)


def persona_path(root: Path, role: str, file: str = "SOUL.md") -> Path:
    """角色卡路径。role 和 file 必须各过一道白名单。

    两个白名单缺一不可：只校验 role 时，`file` 传 `../../config.json`
    就能写到库外面去；只校验 file 时，role 名里的 `..` 同理。
    """
    role_re = getattr(mem, "ROLE_RE", None)
    if role_re is None or not role_re.match(role or ""):
        raise ValueError("角色名不合法")
    if file not in PERSONA_FILES:
        raise ValueError(f"只能写 {' / '.join(PERSONA_FILES)}")
    folder = (root / "roles" / role).resolve()
    if root not in folder.parents or not folder.is_dir():
        raise FileNotFoundError(f"没有 roles/{role}")
    p = folder / file
    if not p.is_file():
        raise FileNotFoundError(f"roles/{role}/{file} 不存在")
    return p


def _check_mtime(p: Path, expect_mtime) -> None:
    """原件在打开后被别人改过就拒绝：前端拿着旧正文覆盖会把对方的改动吞掉。"""
    if expect_mtime is None:
        return
    try:
        expected = float(expect_mtime)
    except (TypeError, ValueError):
        return
    if abs(p.stat().st_mtime - expected) > 0.001:
        raise mem.MetaError("conflict", "文件在打开后被改过，先重新加载再保存")


def write_persona(root: Path, agent: str, file: str, text: str, expect_mtime) -> dict:
    """整份人格文件覆盖写。分段逻辑在前端，服务端只管「原件没被别人改过」。

    0.6.3 起写的是角色卡 `roles/<role>/SOUL.md`：人格跟着角色走，不再跟着
    agent 目录走。没配角色就拒绝——静默写回旧路径会让迁移看起来没做。
    """
    role, role_src = role_of_agent(root, agent)
    # 写必须走显式映射：defaultRole 是给「读」兜底的，拿它写等于几个 agent 共用
    # 一张卡时互相覆盖——一个拼错的 agent id 就能悄悄改掉 advisor 的人格。
    explicit = getattr(mem, "role_map", lambda r: {})(root).get(agent)
    if not role or not explicit:
        raise mem.MetaError(
            "missing",
            f"{agent} 还没配角色卡（config.roles 里没有它）；"
            f"defaultRole 只兜底读，不拿来写——先跑 roles --map {agent} <role>",
        )
    p = persona_path(root, role, file)
    if not isinstance(text, str):
        raise ValueError("text 要是字符串")
    if len(text) > 400000:
        raise ValueError(f"{file} 超过 40 万字符，拒绝写入")
    _check_mtime(p, expect_mtime)
    backup(p, root)
    mem.write(p, text)
    return {"agent": agent, "role": role, "roleSource": role_src,
            "path": f"roles/{role}/{file}",
            "bytes": len(text.encode("utf-8")), "mtime": p.stat().st_mtime}


def write_soul(root: Path, agent: str, text: str, expect_mtime) -> dict:
    """兼容旧路径：`/api/soul` 与 `api.py soul` 都还走这里。"""
    return write_persona(root, agent, "SOUL.md", text, expect_mtime)


def write_text(root: Path, rel: str, text: str, expect_mtime) -> dict:
    """记忆页的自由文本写入（服务端模式，不依赖目录授权）。

    只放 `.md`：这个页面是「看记忆、改记忆」，不是文件管理器；放开后缀等于
    把 config.json 和插件代码也交出去。`.state` / `.git` 另有用途，一律不写。
    """
    p = safe_path(root, rel)
    if p.suffix.lower() != ".md":
        raise ValueError("只允许保存 .md 文件")
    if {".state", ".git"} & set(p.parts):
        raise ValueError(".state / .git 下的文件不给写")
    if not p.is_file():
        raise FileNotFoundError(rel)
    if not isinstance(text, str):
        raise ValueError("text 要是字符串")
    if len(text) > 400000:
        raise ValueError("文件超过 40 万字符，拒绝写入")
    _check_mtime(p, expect_mtime)
    backup(p, root)
    mem.write(p, text)
    return {"path": rel, "bytes": len(text.encode("utf-8")), "mtime": p.stat().st_mtime}


def apply_project_meta(root: Path, data: dict) -> dict:
    """写项目册 `_meta.md` 的可编辑字段。

    抽成函数是为了让 HTTP 处理器与 `ui/api.py`（DSH 内置路由的子进程入口）
    共用同一份写前备份与冲突检测——两处各写一遍，早晚有一处忘了备份。
    """
    book = project_book(str(data.get("path", "")))
    meta = book / "_meta.md"
    fields = data.get("fields") or {}
    backup(meta, root)
    return mem.write_project_meta(meta, fields, expect_mtime=fields.get("mtime"))


def view_playbooks(root: Path) -> dict:
    """`shared/playbooks/` 下每个领域 + 它的 method.md 状态。

    设置页「领域」卡只读这一份：列表和「缺文件」的判断都来自 core，
    前端不自己扫目录——两处各扫一遍，迟早一处认 `.md` 另一处不认。
    """
    return {"root": str(root), "kinds": mem.playbook_dirs(root)}


def apply_playbook(root: Path, data: dict) -> dict:
    """新建领域。四个字段 + 可选 `fill`（只补文件、不动已存在的目录）。"""
    return mem.create_playbook(
        root,
        {
            "kind": data.get("kind"),
            "title": data.get("title"),
            "summary": data.get("summary"),
            "when": data.get("when"),
        },
        fill=bool(data.get("fill")),
    )


class Handler(SimpleHTTPRequestHandler):
    server_version = "memory-ui/1.0"
    root: Path = Path(".")   # make_server 里填，别在别处改
    cwd: Path = Path(".")    # 预演注入时用的会话目录（--cwd，默认启动时的工作目录）

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def log_message(self, fmt, *args):  # noqa: A003
        """静音。默认实现把每次请求都打到终端，API 报错会被淹没。

        真出错时前端会显示错误，服务端不再重复一遍。
        """
        return

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, payload: dict) -> None:
        """成功响应。信封的 `ok` 永远压过业务字段。

        业务返回体里出现同名 `ok` 是个安静的坑：collect_checkup 曾经拿 ok 表示
        「体检通过」，于是没问题时响应变成 `ok: false`，前端把体检结果当成请求
        失败。这里既强制信封生效，又在出现撞名时留一行警告，免得下次再踩。
        """
        if "ok" in payload and payload["ok"] is not True:
            print(
                f"[serve] 警告：响应体带了业务字段 ok={payload['ok']!r}，已按信封覆盖；"
                f"请给业务字段改名（现有键：{', '.join(sorted(payload)[:6])} …）",
                file=sys.stderr,
            )
        self._json({**payload, "ok": True})

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._json({"ok": False, "code": code, "error": message}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 1024 * 1024:
            raise ValueError("请求体太大")
        raw = self.rfile.read(n)
        try:
            # 不是 UTF-8 就是客户端把本地编码直接发过来了（Windows 的 curl -d 常见），
            # 这是请求的问题，不是服务端故障——回 400，别让主人对着 500 猜。
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("请求体不是 UTF-8：Windows 上用 curl 传中文请加 --data-binary @文件，或让页面代发") from None
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("请求体要是 JSON 对象")
        return data

    # ---- GET
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path == "/":
            self.path = "/settings.html"
            return SimpleHTTPRequestHandler.do_GET(self)
        if not parsed.path.startswith("/api/"):
            return SimpleHTTPRequestHandler.do_GET(self)

        root = self.root
        try:
            if parsed.path == "/api/overview":
                return self._ok(view_overview(
                    root, q.get("agent"), q.get("cwd") or str(type(self).cwd)))
            if parsed.path == "/api/tree":
                return self._ok(view_tree(root))
            if parsed.path == "/api/file":
                return self._ok(view_file(root, q.get("path", "")))
            if parsed.path == "/api/config":
                return self._ok(view_config(root))
            if parsed.path == "/api/checkup":
                res = mem.collect_checkup(root, all_projects=q.get("all") == "1")
                return self._ok(res)
            if parsed.path == "/api/project-meta":
                book = project_book(q.get("path", ""))
                return self._ok(mem.read_project_meta(book / "_meta.md"))
            if parsed.path == "/api/playbook":
                return self._ok(view_playbooks(root))
            return self._error(404, f"没有这个接口：{parsed.path}")
        except FileNotFoundError as exc:
            return self._error(404, str(exc) or "文件不存在")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- POST
    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        root = self.root
        if parsed.path not in {"/api/project-meta", "/api/config", "/api/checkup",
                               "/api/playbook", "/api/soul", "/api/persona", "/api/file"}:
            return self._error(404, f"没有这个接口：{parsed.path}")
        try:
            data = self._body()
            if parsed.path == "/api/config":
                # 两条通道共用一条路由：带 order 走「只调顺序」，其余走白名单键。
                # CONFIG_RULES 故意不收 inject.order——那里放行等于允许清空注入。
                if "order" in data:
                    return self._ok(mem.set_inject_order(root, data.get("order")))
                return self._ok(apply_config_patch(root, data.get("patch") or data))
            if parsed.path == "/api/checkup":
                return self._ok(mem.apply_fixes(
                    root, data.get("kinds") or [], dry=bool(data.get("dry"))))
            if parsed.path == "/api/playbook":
                return self._ok(apply_playbook(root, data))
            if parsed.path == "/api/soul":
                return self._ok(write_soul(
                    root, str(data.get("agent", "")), data.get("text", ""), data.get("mtime")))
            if parsed.path == "/api/persona":
                return self._ok(write_persona(
                    root, str(data.get("agent", "")), str(data.get("file") or "SOUL.md"),
                    data.get("text", ""), data.get("mtime")))
            if parsed.path == "/api/file":
                return self._ok(write_text(
                    root, str(data.get("path", "")), data.get("text", ""), data.get("mtime")))

            return self._ok(apply_project_meta(root, data))
        except mem.MetaError as exc:
            status = {"invalid": 400, "conflict": 409, "missing": 404}.get(exc.code, 400)
            return self._error(status, exc.message, exc.code)
        except FileNotFoundError as exc:
            return self._error(404, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"{type(exc).__name__}: {exc}")


def make_server(root: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                tries: int = PORT_TRIES) -> ThreadingHTTPServer:
    """绑定端口。8765 被占就往后挪，最多试 tries 次。"""
    Handler.root = root
    last = None
    for i in range(tries):
        p = port + i
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            httpd.daemon_threads = True
            return httpd
        except OSError as exc:
            last = exc
            continue
    raise SystemExit(f"{host}:{port}–{port + tries - 1} 都绑不上：{last}")


# ---------------------------------------------------------------- 自检

def _get(url: str, expect: int = 200) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def self_test(root: Path) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 1. 静态文件不再引用已拆掉的 shared/domains
    html = (HERE / "settings.html").read_text(encoding="utf-8")
    check("settings.html 不引用 shared/domains", "shared/domains" not in html)
    check("七个页签都在", all(f'data-tab="{t}"' in html
                              for t in ("overview", "project", "memory", "persona",
                                        "user", "checkup", "config")))
    # 元信息卡从人格页搬到项目册页：两处都在反而说明搬了一半
    check("元信息卡在项目册页、不在人格页",
          'id="tab-project"' in html and 'id="pmTitle"' in html
          and html.index('id="pmTitle"') < html.index('id="tab-memory"')
          and 'id="pmKindRoute"' in html)

    # 样式表注释漏一个 */ 会把后面几十条规则整段吞掉（踩过：卡片全变白底无边框，页面看着像没加载样式）
    css = (HERE / "style.css").read_text(encoding="utf-8")
    opens, closes = css.count("/*"), css.count("*/")
    check("style.css 注释成对闭合", opens == closes, f"{opens} 开 / {closes} 闭")
    check("style.css 里 .card 规则还在", ".card {" in css)

    # 2. 渲染器：能跑，且不写状态文件（不能因为打开一次设置页就伪造「刚读过」）
    state_dir = root / ".state"
    before = {}
    if state_dir.is_dir():
        for p in state_dir.glob("*.json"):
            try:
                before[p.name] = p.stat().st_mtime_ns
            except OSError:
                pass
    try:
        info = mem.render_injected(root)
        ok = info["chars"] > 0
        detail = f"{info['chars']}/{info['maxChars']} 字符（{info['agent']}）"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    after = {}
    if state_dir.is_dir():
        for p in state_dir.glob("*.json"):
            try:
                after[p.name] = p.stat().st_mtime_ns
            except OSError:
                pass
    check("注入渲染器跑得动", ok, detail)
    check("渲染不改 .state（纯读）", before == after)

    # 3. 起一个临时端口上的真服务
    httpd = make_server(root, port=0, tries=1)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        code, data = _get(f"{base}/api/overview")
        ok = code == 200 and data.get("ok") and set(
            ("root", "injected", "projects", "focus", "config")) <= set(data)
        check("GET /api/overview 契约", ok, f"{code}")
        ov = data

        # 预演身份：config 里的 defaultAgent 可能指向已删除的目录，必须回退到真实存在的
        code, data = _get(f"{base}/api/overview?agent=definitely-not-an-agent")
        names = mem.list_agents(root)
        ok = (code == 200 and data.get("agent") in names
              and bool(data.get("agentFellBack")) == bool(names))
        check("不存在的身份回退到真实目录", ok, f"{data.get('agent')!r}")

        # 预演 cwd：从子目录往上找项目册，kind 才不是空的（否则预演永远显示「没 kind」）
        import tempfile as _tf
        with _tf.TemporaryDirectory() as cwd_tmp:
            ws2 = Path(cwd_tmp)
            (ws2 / ".memory").mkdir()
            (ws2 / ".memory" / "_meta.md").write_text(
                "---\nid: cwdtest\ntitle: 目录测试\nstatus: 进行中\nkind: engineering\n---\n",
                encoding="utf-8")
            start = (ws2 / "sub").as_posix()
            code, data = _get(f"{base}/api/overview?cwd=" + urllib.parse.quote(start))
            got_book = data.get("projectBook") or ""
            ok = (code == 200 and Path(got_book) == ws2 / ".memory"
                  and (data.get("injected") or {}).get("kind") == "engineering")
            check("cwd 向上找到项目册（kind 跟着来）", ok, f"{got_book!r}")

        # 日志天数：跨项目册聚合，只看总册 agents/<id>/log 会读到过期数据
        books = [b for b in mem.project_books(root) if b.get("state") == "ok"]
        expect = {}
        for name in names:
            days = set()
            for d in [root / "agents" / name / "log"] + [Path(b["book"]) / "log" / name for b in books]:
                if d.is_dir():
                    days |= {mem.log_day(f.name) or f.stem for f in d.glob("*.md")}
            expect[name] = len(days)
        got = {a["id"]: a["logDays"] for a in (ov.get("agents") or [])}
        check("日志天数跨项目册聚合", got == expect, f"{got}")

        code, data = _get(f"{base}/api/checkup")
        # 信封 ok 必须仍是 True：业务字段撞名 ok 会让前端把「体检通过」当成请求失败（踩过一次）
        ok = code == 200 and data.get("ok") is True and isinstance(
            data.get("problems"), list) and isinstance(data.get("topActions"), list)
        check("GET /api/checkup 契约（信封 ok 未被业务字段顶掉）", ok, f"{len(data.get('problems') or [])} 条")
        check("checkup 的业务「干净」字段没叫 ok", "clean" in data and isinstance(data["clean"], bool))

        src = (HERE / "serve.py").read_text(encoding="utf-8")
        needle = '{"ok": True,' + " **"  # 拆开写，免得这行自己变成匹配目标
        check("所有成功响应都走 _ok（不再手写 ok 信封）", needle not in src)

        code, data = _get(f"{base}/api/tree")
        files = data.get("files") or []
        ok = code == 200 and files and all(f["path"].endswith(".md") for f in files)
        check("GET /api/tree 只列 md", ok, f"{len(files)} 个文件")

        code, data = _get(f"{base}/api/file?path=" + urllib.parse.quote("../config.json"))
        check("路径越界被拒", code == 400, f"{code}")

        # 3b. 两条写入通道的成功路径：只验拒绝不验成功，等于没验过真能写
        bdir = root / ".state" / "ui-backups"
        backups_before = set(bdir.glob("*.bak")) if bdir.is_dir() else set()
        # 人格自 0.6.3 起写在角色卡上：临时造一张卡 + 一条临时映射，测完还原 config
        cfg_p = root / "config.json"
        cfg_orig = cfg_p.read_text(encoding="utf-8") if cfg_p.is_file() else None
        tmp_agent = root / "agents" / "zz-selftest"
        tmp_role = root / "roles" / "zz-selftest"
        tmp_md = root / "shared" / "zz-selftest.md"

        def _set_roles(mapping):
            cfg_now = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.is_file() else {}
            cfg_now["roles"] = {} if mapping is None else {**(cfg_now.get("roles") or {}), **mapping}
            cfg_p.write_text(
                json.dumps(cfg_now, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

        try:
            tmp_agent.mkdir(parents=True, exist_ok=True)
            tmp_role.mkdir(parents=True, exist_ok=True)
            soul = tmp_role / "SOUL.md"
            soul.write_text("---\nname: 自检\n---\n\n# SOUL.md\n\n原文\n", encoding="utf-8")
            _set_roles({"zz-selftest": "zz-selftest"})
            code, data = _post(f"{base}/api/persona", {
                "agent": "zz-selftest", "file": "SOUL.md",
                "text": "---\nname: 自检改\n---\n\n# SOUL.md\n\n原文\n",
                "mtime": soul.stat().st_mtime})
            check("POST /api/persona 写的是角色卡（roles/<role>/SOUL.md）",
                  code == 200 and "自检改" in soul.read_text(encoding="utf-8")
                  and data.get("path") == "roles/zz-selftest/SOUL.md"
                  and data.get("role") == "zz-selftest",
                  f"{code} {data.get('path') or data.get('error', '')}")

            # 没配角色的 agent 一律拒写：静默写回旧路径会让迁移看起来没做
            _set_roles(None)
            code, _ = _post(f"{base}/api/persona", {
                "agent": "zz-selftest", "file": "SOUL.md", "text": "x"})
            check("没配角色的 agent 拒写人格", code == 404, f"{code}")
            _set_roles({"zz-selftest": "zz-selftest"})

            # IDENTITY.md 已退役：还能写它说明 PERSONA_FILES 没收窄
            code, _ = _post(f"{base}/api/persona", {
                "agent": "zz-selftest", "file": "IDENTITY.md", "text": "x"})
            check("POST /api/persona 拒收 IDENTITY.md", code == 400, f"{code}")

            # roles --new / --map / --default：建卡不覆盖、映射落 config、defaultRole 兜底
            new_card = root / "roles" / "zz-new" / "SOUL.md"
            rc = mem.roles_new(root, ["--new", "zz-new", "--blank"])
            check("roles --new 建卡（--blank）",
                  rc == 0 and new_card.is_file() and "name: zz-new" in new_card.read_text(encoding="utf-8"),
                  f"rc={rc}")
            new_card.write_text("MARK\n", encoding="utf-8")
            rc = mem.roles_new(root, ["--new", "zz-new", "--blank"])
            check("roles --new 不覆盖已有卡",
                  rc == 1 and new_card.read_text(encoding="utf-8") == "MARK\n", f"rc={rc}")
            rc = mem.roles_map_agent(root, ["--map", "zz-selftest", "zz-new"])
            cfg_after = json.loads(cfg_p.read_text(encoding="utf-8"))
            check("roles --map 写 config.roles",
                  rc == 0 and (cfg_after.get("roles") or {}).get("zz-selftest") == "zz-new",
                  f"rc={rc} {(cfg_after.get('roles') or {}).get('zz-selftest')}")
            rc = mem.roles_set_default(root, ["--default", "zz-new"])
            cfg_after = json.loads(cfg_p.read_text(encoding="utf-8"))
            role_fb, _src = mem.resolve_role(root, "zz-unmapped", cfg_after)
            check("defaultRole 兜底：未映射的 agent 也解析出角色",
                  rc == 0 and cfg_after.get("defaultRole") == "zz-new" and role_fb == "zz-new",
                  f"rc={rc} role={role_fb}")

            tmp_md.write_text("# 自检\n", encoding="utf-8")
            code, data = _post(f"{base}/api/file", {
                "path": "shared/zz-selftest.md", "text": "# 自检改\n",
                "mtime": tmp_md.stat().st_mtime})
            check("POST /api/file 正常保存",
                  code == 200 and tmp_md.read_text(encoding="utf-8") == "# 自检改\n",
                  f"{code} {data.get('error', '')}")

            backups_after = set(bdir.glob("*.bak")) if bdir.is_dir() else set()
            check("写入前留了备份", len(backups_after - backups_before) >= 2,
                  f"{len(backups_after - backups_before)} 份")
        finally:
            shutil.rmtree(tmp_agent, ignore_errors=True)
            shutil.rmtree(tmp_role, ignore_errors=True)
            shutil.rmtree(root / "roles" / "zz-new", ignore_errors=True)
            if cfg_orig is not None:
                cfg_p.write_text(cfg_orig, encoding="utf-8")
            else:
                cfg_p.unlink(missing_ok=True)
            tmp_md.unlink(missing_ok=True)
            for p in (set(bdir.glob("*.bak")) if bdir.is_dir() else set()) - backups_before:
                p.unlink(missing_ok=True)

        # 4. 结构化写入：幂等、改单字段、冲突检测
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            book = ws / ".memory"
            book.mkdir()
            meta = book / "_meta.md"
            original = (
                "---\n"
                "id: selftest\n"
                "title: 自检册\n"
                "status: 进行中\n"
                "kind: engineering\n"
                "created: 2026-01-01\n"
                "updated: 2026-01-01\n"
                "---\n\n"
                "# 自检册\n\n"
                "## 定位\n\n"
                "自检用，别当真。\n\n"
                "## 注意事项\n\n"
                "正文导语。\n\n"
                "**禁区**\n\n"
                "别动这里。\n\n"
                "**读取提示**\n\n"
                "先读 core.md。\n"
            )
            meta.write_text(original, encoding="utf-8")
            code, meta_data = _get(f"{base}/api/project-meta?path=" + urllib.parse.quote(ws.as_posix()))
            ok = code == 200 and meta_data.get("status") == "进行中" and meta_data.get("forbidden") == "别动这里。"
            check("GET /api/project-meta 解析三块注意事项", ok, f"{code}")

            noop = {
                "path": ws.as_posix(),
                "fields": {
                    "mtime": meta_data.get("mtime"),
                    "status": "进行中",
                    "position": "自检用，别当真。",
                    "forbidden": "别动这里。",
                    "hints": "先读 core.md。",
                },
            }
            code, data = _post(f"{base}/api/project-meta", noop)
            same = meta.read_text(encoding="utf-8") == original
            check("同值保存 = 零改动（字节不变）", code == 200 and data.get("changed") == [] and same,
                  f"{code} changed={data.get('changed')}")

            edit = {"path": ws.as_posix(), "fields": {"mtime": meta_data.get("mtime"), "status": "搁置"}}
            code, data = _post(f"{base}/api/project-meta", edit)
            text = meta.read_text(encoding="utf-8")
            check("单字段改 status", code == 200 and data.get("changed") == ["status"]
                  and "status: 搁置" in text, f"{code} {data.get('changed')}")
            check("改 status 不动正文其它字节",
                  "**禁区**\n\n别动这里。" in text and "自检用，别当真。" in text)

            stale = {"path": ws.as_posix(), "fields": {"mtime": 1.0, "status": "进行中"}}
            code, data = _post(f"{base}/api/project-meta", stale)
            check("mtime 冲突被拒（409）", code == 409, f"{code} {data.get('code')}")

            bad = {"path": ws.as_posix(), "fields": {"kind": "../evil"}}
            code, data = _post(f"{base}/api/project-meta", bad)
            check("非法 kind 被拒（400）", code == 400, f"{code}")

        # 5. 配置写入：空 patch 不写文件
        cfg_path = root / "config.json"
        before_cfg = cfg_path.read_bytes() if cfg_path.is_file() else b""
        code, data = _post(f"{base}/api/config", {"patch": {}})
        after_cfg = cfg_path.read_bytes() if cfg_path.is_file() else b""
        check("空 patch 不落盘", code == 200 and data.get("changed") == [] and before_cfg == after_cfg,
              f"{code}")

        code, data = _post(f"{base}/api/config", {"patch": {"inject": {"maxChars": 10}}})
        check("越界配置被拒（400）", code == 400, f"{code}")

        code, data = _post(f"{base}/api/config", {"patch": {"inject": {"order": []}}})
        check("非白名单键被忽略", code == 200 and data.get("changed") == [], f"{code}")

        # 6. 一键修：HTTP 只验拒绝路径（写路径在临时根上验，别拿真库当试验田）
        code, data = _post(f"{base}/api/checkup", {"kinds": ["bogus"]})
        check("修复杂项被拒（400）", code == 400, f"{code} {data.get('error', '')}")

        order_paths = [e.get("path") for e in view_config(root)["config"].get("inject", {}).get("order", [])
                       if e.get("path")]
        if order_paths:
            code, _ = _post(f"{base}/api/config", {"order": order_paths[:-1]})
            check("顺序少一条被拒（400）", code == 400, f"{code}")
            code, _ = _post(f"{base}/api/config", {"order": [order_paths[0]] * len(order_paths)})
            check("顺序重复被拒（400）", code == 400, f"{code}")

        # 7. 修复与重排在临时根上跑：索引漂移 → 修完手写行还在；priority 连续编号
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp) / "mem"
            (troot / "shared").mkdir(parents=True)
            ws = Path(tmp) / "ws"
            (ws / ".memory").mkdir(parents=True)
            (ws / ".memory" / "_meta.md").write_text(
                "---\nid: fixme\ntitle: 自检修复\nstatus: 进行中\nkind: engineering\n---\n\n## 定位\n\n自检。\n",
                encoding="utf-8")
            mem.write_workspaces(troot, [{"id": "fixme", "path": ws.as_posix()}])
            idx = troot / "shared" / "index.md"
            idx.write_text(
                "# 索引\n\n## 项目册（事实源）\n\n| 工作区 | 册子 | 装什么 |\n| --- | --- | --- |\n"
                "| `E:/hand/written` | 手写行 | 别删 |\n\n"
                "## playbooks\n\n| 文件 | 装什么 | 何时读 |\n| --- | --- | --- |\n\n"
                "## topics\n\n| 文件 | 装什么 | 何时读 |\n| --- | --- | --- |\n",
                encoding="utf-8")
            tcfg = json.loads(json.dumps(mem.CONFIG_DEFAULTS))
            tcfg["sync"]["enabled"] = True
            tcfg["legacyKey"] = 1
            tcfg["inject"]["order"] = [
                {"path": "a.md", "priority": 5},
                {"path": "b.md", "priority": 3},
                {"path": "c.md", "priority": 9, "required": True},
            ]
            (troot / "config.json").write_text(
                json.dumps(tcfg, ensure_ascii=False, indent=2), encoding="utf-8")

            mtime_before = idx.stat().st_mtime_ns
            dry = mem.apply_fixes(troot, ["index_sync", "config_sync_off"], dry=True)
            check("--dry 不动文件",
                  idx.stat().st_mtime_ns == mtime_before and bool(dry.get("applied"))
                  and str(dry["applied"][0].get("detail", "")).startswith("将执行"),
                  f"{dry.get('applied')}")
            res = mem.apply_fixes(troot, ["index_sync", "config_sync_off", "config_drop_unknown"])
            text = idx.read_text(encoding="utf-8")
            saved = json.loads((troot / "config.json").read_text(encoding="utf-8"))
            check("修索引：补上项目册行、手写行保留",
                  ws.as_posix() in text and "E:/hand/written" in text,
                  f"{len(res.get('applied') or [])} 项")
            check("修配置：sync 关掉、未知键删掉",
                  saved["sync"]["enabled"] is False and "legacyKey" not in saved)

            mem.set_inject_order(troot, ["c.md", "a.md", "b.md"])
            saved = json.loads((troot / "config.json").read_text(encoding="utf-8"))
            got = [(e["path"], e["priority"]) for e in saved["inject"]["order"]]
            check("重排后 priority = 1..N", got == [("c.md", 1), ("a.md", 2), ("b.md", 3)], str(got))
            check("重排保留 required 等标记", saved["inject"]["order"][0].get("required") is True)
            for bad_paths, why in ((["c.md", "a.md"], "缺"), (["c.md", "a.md", "b.md", "d.md"], "多"),
                                   (["a.md", "a.md", "b.md"], "重复")):
                try:
                    mem.set_inject_order(troot, bad_paths)
                    check(f"顺序{why}被拒", False, "居然放过了")
                except ValueError:
                    check(f"顺序{why}被拒", True)

        # 8. 两套主题必须覆盖同一组变量，且 token 区之外不许有颜色字面量
        css = (Path(__file__).parent / "style.css").read_text(encoding="utf-8")

        def _token_block(marker: str) -> str:
            i = css.index(marker)
            return css[i:css.index("}", i)]

        def _var_names(block: str) -> set:
            return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

        dark_names = _var_names(_token_block(":root {"))
        light_names = _var_names(_token_block(':root[data-theme="light"] {'))
        check("亮色覆盖了所有主题变量", dark_names == light_names,
              f"暗色 {len(dark_names)} 个 vs 亮色 {len(light_names)} 个")

        stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        stripped = stripped.replace(_token_block(":root {"), "")
        stripped = stripped.replace(_token_block(':root[data-theme="light"] {'), "")
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", stripped)
        check("token 区之外没有颜色字面量", not literals, ", ".join(literals[:5]))

        # 9. 领域（shared/playbooks/<kind>/method.md）：HTTP 只验拒绝路径，
        #    真建文件在临时根上试——自检不该往真库里添东西。
        code, data = _get(f"{base}/api/playbook")
        plist = data.get("kinds") or []
        ok = (code == 200 and isinstance(plist, list)
              and all({"kind", "has_method"} <= set(k) for k in plist))
        check("GET /api/playbook 列出领域与文件状态", ok, f"{len(plist)} 个")

        code, _ = _post(f"{base}/api/playbook",
                        {"kind": "../evil", "title": "x", "summary": "y", "when": "z"})
        check("非法领域名被拒（400）", code == 400, f"{code}")
        code, _ = _post(f"{base}/api/playbook",
                        {"kind": "zz-selftest", "title": "自检", "summary": "", "when": "自检时"})
        check("空说明被拒（400）", code == 400, f"{code}")

        # 客户端按本地编码发请求体（Windows 上 curl -d 中文就这样）→ 400，不是 500
        raw_req = urllib.request.Request(
            f"{base}/api/playbook",
            data=json.dumps({"kind": "x"}, ensure_ascii=False).encode("gbk"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(raw_req, timeout=10) as r:
                bad_code = r.status
        except urllib.error.HTTPError as exc:
            bad_code = exc.code
        check("请求体不是 UTF-8 → 400（不是 500）", bad_code == 400, str(bad_code))

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp) / "mem"
            eng = troot / "shared" / "playbooks" / "engineering"
            eng.mkdir(parents=True)
            (eng / "method.md").write_text(
                "---\ntitle: 工程\nsummary: 写代码\nwhen: 动代码时\nowner: ai\n---\n\n正文\n",
                encoding="utf-8")
            (troot / "shared" / "playbooks" / "halfdone").mkdir()
            idx2 = troot / "shared" / "index.md"
            idx2.write_text(
                "# 索引\n\n## 项目册（事实源）\n\n| 工作区 | 册子 | 装什么 |\n| --- | --- | --- |\n\n"
                "## playbooks\n\n| 文件 | 装什么 | 何时读 |\n| --- | --- | --- |\n\n"
                "## topics\n\n| 文件 | 装什么 | 何时读 |\n| --- | --- | --- |\n",
                encoding="utf-8")
            res = mem.create_playbook(troot, {
                "kind": "embodied", "title": "具身智能",
                "summary": "机器人 / 强化学习", "when": "规划选题时"})
            made = troot / "shared" / "playbooks" / "embodied" / "method.md"
            text2 = idx2.read_text(encoding="utf-8")
            check("新建领域：文件落盘 + 索引自动补行",
                  made.is_file() and "embodied/method.md" in text2
                  and res.get("path") == "shared/playbooks/embodied/method.md",
                  f"index_synced={res.get('index_synced')}")
            check("新建领域：目录表里认出 method.md",
                  any(d["kind"] == "embodied" and d["has_method"] for d in mem.playbook_dirs(troot)))

            try:
                mem.create_playbook(troot, {
                    "kind": "embodied", "title": "再来一次", "summary": "y", "when": "z"})
                check("重复新建被拒", False, "居然覆盖了")
            except mem.MetaError as exc:
                check("重复新建被拒（conflict）", exc.code == "conflict", exc.code)

            try:
                mem.create_playbook(troot, {
                    "kind": "halfdone", "title": "半成品", "summary": "y", "when": "z"})
                check("目录已在、缺文件时先要 fill", False, "没要 fill 就写了")
            except mem.MetaError as exc:
                check("目录已在、缺文件时先要 fill（conflict）", exc.code == "conflict", exc.code)
            mem.create_playbook(troot, {
                "kind": "halfdone", "title": "半成品", "summary": "y",
                "when": "z"}, fill=True)
            check("fill 只补文件",
                  (troot / "shared" / "playbooks" / "halfdone" / "method.md").is_file()
                  and (troot / "shared" / "playbooks" / "halfdone").is_dir())

            warned = mem.create_playbook(troot, {
                "kind": "vague", "title": "空话", "summary": "一些等等", "when": "动代码时"})
            check("空话 summary / when 撞车只提醒不拦",
                  warned.get("warnings") and len(warned["warnings"]) >= 2,
                  str(warned.get("warnings")))

            for bad_kind in ("../x", "Bad Name", "有空格", ""):
                try:
                    mem.create_playbook(troot, {
                        "kind": bad_kind, "title": "x", "summary": "y", "when": "z"})
                    check(f"领域名 {bad_kind!r} 被拒", False, "居然写了")
                except mem.MetaError as exc:
                    check(f"领域名 {bad_kind!r} 被拒（invalid）", exc.code == "invalid", exc.code)
        # 10. 角色卡：新 agent 走卡不走旧目录；两条体检（写坏的映射、孤儿卡）
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp) / "mem"
            troot.mkdir()
            mem.cmd_init(troot)
            rc = mem.cmd_init_agent(troot, "zz-new-dsh", role="zz-role")
            card = troot / "roles" / "zz-role" / "SOUL.md"
            saved = json.loads((troot / "config.json").read_text(encoding="utf-8"))
            check("init --agent 写角色卡，不写旧的 agents/<id>/SOUL.md",
                  rc == 0 and card.is_file()
                  and not (troot / "agents" / "zz-new-dsh" / "SOUL.md").exists()
                  and (saved.get("roles") or {}).get("zz-new-dsh") == "zz-role")
            rc2 = mem.cmd_init_agent(troot, "zz-second-dsh", role="zz-role")
            check("--role 指向已有卡时沿用，不覆盖", rc2 == 0 and card.is_file())

            (troot / "roles" / "zz-orphan").mkdir()
            (troot / "roles" / "zz-orphan" / "SOUL.md").write_text(
                "---\nname: 孤儿\nidentity: 没人用\n---\n\n## 原则\n", encoding="utf-8")
            cfg = json.loads((troot / "config.json").read_text(encoding="utf-8"))
            cfg["roles"]["zz-new-dsh"] = "Bad Role"
            (troot / "config.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            report = mem.collect_checkup(troot)
            msgs = [p["desc"] for p in report["problems"]]
            check("体检报出写坏的 config.roles", any("不合法" in m for m in msgs), str(msgs[:2]))
            check("体检报出没人用的角色卡", any("没有任何 agent 指向" in m for m in msgs))

    finally:
        httpd.shutdown()
        httpd.server_close()

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  — {detail}" if detail else ""))
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


# ---------------------------------------------------------------- 入口

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="记忆库设置页的本地服务")
    ap.add_argument("--root", help="记忆库路径（默认 ~/.memory 或 MEMORY_DIR）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--cwd", help="预演注入时假装从哪个目录开会话（默认当前目录）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv[1:])

    root = Path(args.root).expanduser().resolve() if args.root else mem.find_root()

    if args.self_test:
        print(f"自检：{root}")
        return self_test(root)

    if not root.is_dir():
        print(f"记忆库不存在：{root}")
        print("用 `python core/memory.py init` 建一个，或 --root 指定别处。")
        return 1

    httpd = make_server(root, args.host, args.port)
    Handler.root = root
    Handler.cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    port = httpd.server_address[1]
    url = f"http://localhost:{port}/settings.html"
    print(f"记忆库：{root}")
    print(f"设置页：{url}")
    print("Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
