#!/usr/bin/env python3
"""单次调用的 API 入口：给 DSH 插件用（请求进来起一个 Python，跑完就退）。

为什么不是常驻服务：内置进 DSH 后，页面已经挂在宿主自己的端口下，再起一个
127.0.0.1:8765 就只是把旧方案藏在后面——多一个进程、多一个端口、多一处
生命周期要管。`ui/serve.py` 仍然保留：它给「不想装 DSH」的场景用，而且自检
（`python ui/serve.py --self-test`）跑在它身上。

两个入口共用同一批函数（`view_*` / `apply_*`），不各自实现一遍——这是
2026-10-27 修 UI 时定下的硬约束：判定只许有一份。

约定：

  stdin     可选，POST 的 JSON 对象
  stdout    恰好一行 JSON：`{"status":200,"ok":true,"value":...}`
            失败时 `{"status":400,"ok":false,"error":"...","code":"..."}`
  退出码    0 = 跑完了（**不代表请求成功**，看 JSON 的 status）；
            2 = 用法错误；1 = 未捕获异常

端点：

  overview [--cwd DIR] [--agent ID]
  tree
  file --path REL [--write body]
  config [body]                带 order 改注入顺序，否则改白名单键
  checkup [--all]              加 --write 时执行修复（body: kinds/dry）
  project-meta [--path WS] [body]
  playbook                     列领域与 method.md 状态
  playbook --write body        新建领域（body: kind/title/summary/when/fill）
  soul body
  persona body
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "core"))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import serve  # noqa: E402  （顺带把 core/memory.py 加载成 serve.mem）

mem = serve.mem


def flag(argv: list[str], name: str) -> str | None:
    """取 `--name value`。写法与 core/memory.py 的 flag() 一致。"""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    for item in argv:
        if item.startswith(name + "="):
            return item[len(name) + 1:]
    return None


def read_body(argv: list[str]) -> dict:
    """POST 的 JSON 对象。

    插件那边用 `--body-file` 传：DSH 的 subprocess 封装没有给 stdin 留稳定
    接口，临时文件是两边都能理解的最小契约。stdin 仍然收，方便手工调试。
    """
    path = flag(argv, "--body-file")
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    if raw.strip() == "":
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("请求体要是 JSON 对象")
    return data


def dispatch(name: str, argv: list[str]) -> tuple[int, dict]:
    root = mem.find_root()
    if not root.is_dir():
        raise FileNotFoundError(f"记忆库不存在：{root}")

    if name == "overview":
        cwd = flag(argv, "--cwd") or str(Path.cwd())
        return 200, serve.view_overview(root, flag(argv, "--agent"), cwd)
    if name == "tree":
        return 200, serve.view_tree(root)
    if name == "file":
        if flag(argv, "--write") is not None:
            body = read_body(argv)
            return 200, serve.write_text(
                root, str(body.get("path") or flag(argv, "--path") or ""),
                body.get("text", ""), body.get("mtime"))
        return 200, serve.view_file(root, flag(argv, "--path") or "")
    if name == "checkup":
        if flag(argv, "--write") is not None:
            body = read_body(argv)
            return 200, mem.apply_fixes(
                root, body.get("kinds") or [], dry=bool(body.get("dry")))
        return 200, mem.collect_checkup(root, all_projects=flag(argv, "--all") in ("1", "true", "yes"))
    if name == "config":
        if flag(argv, "--write") is not None:
            body = read_body(argv)
            if "order" in body:
                return 200, mem.set_inject_order(root, body.get("order"))
            return 200, serve.apply_config_patch(root, body.get("patch") or body)
        return 200, serve.view_config(root)
    if name == "project-meta":
        if flag(argv, "--write") is not None:
            return 200, serve.apply_project_meta(root, read_body(argv))
        book = serve.project_book(flag(argv, "--path") or "")
        return 200, mem.read_project_meta(book / "_meta.md")
    if name == "playbook":
        if flag(argv, "--write") is not None:
            return 200, serve.apply_playbook(root, read_body(argv))
        return 200, serve.view_playbooks(root)
    if name == "soul":
        body = read_body(argv)
        return 200, serve.write_soul(
            root, str(body.get("agent", "")), body.get("text", ""), body.get("mtime"))
    if name == "persona":
        body = read_body(argv)
        return 200, serve.write_persona(
            root, str(body.get("agent", "")), str(body.get("file") or "SOUL.md"),
            body.get("text", ""), body.get("mtime"))

    raise ValueError(f"没有这个接口：{name}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0 if argv else 2

    name, rest = argv[0], argv[1:]
    try:
        status, value = dispatch(name, rest)
        out = {"status": status, "ok": True, "value": value}
    except mem.MetaError as exc:
        status = {"invalid": 400, "conflict": 409, "missing": 404}.get(exc.code, 400)
        out = {"status": status, "ok": False, "error": exc.message, "code": exc.code}
    except FileNotFoundError as exc:
        out = {"status": 404, "ok": False, "error": str(exc) or "文件不存在", "code": "missing"}
    except ValueError as exc:
        out = {"status": 400, "ok": False, "error": str(exc), "code": "invalid"}
    except Exception as exc:  # noqa: BLE001 —— 单次进程，报清楚比栈更重要
        out = {"status": 500, "ok": False, "error": f"{type(exc).__name__}: {exc}", "code": "internal"}

    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
