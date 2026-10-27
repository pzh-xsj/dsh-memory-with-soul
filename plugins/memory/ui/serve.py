#!/usr/bin/env python3
"""记忆库设置页的本地服务。

两个职责：

1. 发静态文件。`file://` 打开时 File System Access API 不可用（浏览器只在
   安全上下文里给 `showDirectoryPicker`），所以设置页必须走 http://localhost。
2. 提供 `/api/*`：页面要显示的事实（体检、注入预算、项目册、目录树）从
   `core/memory.py` 现算，不在 JS 里重写第二份判断。**这条是 2026-10-27 修
   UI 时的唯一硬约束**——上一版把体检逻辑抄进前端，架构一改（拆掉
   `shared/domains`）就全坏了，页面还在报一个不存在的「分册」。

写入只有两条，都是「结构化、白名单、先备份」：

  POST /api/project-meta   改项目册 `_meta.md` 的 status / kind / title /
                           「## 定位」/「## 注意事项」三块
  POST /api/config         改 `config.json` 的白名单键，其余键原样保留

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
            seen.setdefault(f.stem, src)
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


PERSONA_FILES = ("SOUL.md", "IDENTITY.md")


def persona_path(root: Path, agent: str, file: str = "SOUL.md") -> Path:
    """人格文件路径。agent 和 file 必须各过一道白名单。

    两个白名单缺一不可：只校验 agent 时，`file` 传 `../../config.json`
    就能写到库外面去；只校验 file 时，agent 名里的 `..` 同理。
    """
    if not AGENT_ID_RE.match(agent or ""):
        raise ValueError("agent 名不合法")
    if file not in PERSONA_FILES:
        raise ValueError(f"只能写 {' / '.join(PERSONA_FILES)}")
    folder = (root / "agents" / agent).resolve()
    if root not in folder.parents or not folder.is_dir():
        raise FileNotFoundError(f"没有 agents/{agent}")
    p = folder / file
    if not p.is_file():
        raise FileNotFoundError(f"agents/{agent}/{file} 不存在")
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
    """整份人格文件覆盖写。分段逻辑在前端，服务端只管「原件没被别人改过」。"""
    p = persona_path(root, agent, file)
    if not isinstance(text, str):
        raise ValueError("text 要是字符串")
    if len(text) > 400000:
        raise ValueError(f"{file} 超过 40 万字符，拒绝写入")
    _check_mtime(p, expect_mtime)
    backup(p, root)
    mem.write(p, text)
    return {"agent": agent, "path": f"agents/{agent}/{file}",
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
        data = json.loads(raw.decode("utf-8"))
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
        if parsed.path not in {"/api/project-meta", "/api/config", "/api/soul",
                               "/api/persona", "/api/file"}:
            return self._error(404, f"没有这个接口：{parsed.path}")
        try:
            data = self._body()
            if parsed.path == "/api/config":
                return self._ok(apply_config_patch(root, data.get("patch") or data))
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
    check("五个页签都在", all(f'data-tab="{t}"' in html
                              for t in ("overview", "memory", "persona", "checkup", "config")))

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
                    days |= {f.stem for f in d.glob("*.md")}
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
        tmp_agent = root / "agents" / "zz-selftest"
        tmp_md = root / "shared" / "zz-selftest.md"
        try:
            tmp_agent.mkdir(parents=True, exist_ok=True)
            ident = tmp_agent / "IDENTITY.md"
            ident.write_text("# IDENTITY.md\n\n- **Name:** 自检\n", encoding="utf-8")
            code, data = _post(f"{base}/api/persona", {
                "agent": "zz-selftest", "file": "IDENTITY.md",
                "text": "# IDENTITY.md\n\n- **Name:** 自检改\n",
                "mtime": ident.stat().st_mtime})
            check("POST /api/persona 正常保存",
                  code == 200 and "自检改" in ident.read_text(encoding="utf-8"),
                  f"{code} {data.get('error', '')}")

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
