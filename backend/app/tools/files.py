"""会话目录内的路径检查和文件工具。"""

import re
from pathlib import Path


READ_LIMIT = 50000
LIST_LIMIT = 200
_FILE_BYTE_LIMIT = 2_000_000


class PathRejected(Exception):
    def __init__(self, message: str = "路径越界"):
        super().__init__(message)
        self.message = message


def session_directory(root: Path, session_id: str) -> Path:
    if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        raise PathRejected("会话无效")
    base = root.resolve()
    path = (base / session_id).resolve()
    if path.parent != base:
        raise PathRejected("会话无效")
    return path


def resolve_inside(root: Path, raw: str) -> Path:
    text = raw.strip()
    if not text:
        raise PathRejected("路径为空")
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PathRejected("路径越界")
    base = root.resolve()
    full = (base / candidate).resolve()
    try:
        full.relative_to(base)
    except ValueError as exc:
        raise PathRejected("路径越界") from exc
    return full


def _fail(message: str) -> dict:
    return {"text": message, "is_error": True}


def _ok(text: str, **extra) -> dict:
    return {"text": text, "is_error": False, **extra}


def _locate(root: Path, raw: object) -> Path | dict:
    if not isinstance(raw, str):
        return _fail("路径为空")
    try:
        return resolve_inside(root, raw)
    except PathRejected as exc:
        return _fail(exc.message)


def _read_utf8(path: Path) -> str | dict:
    if path.stat().st_size > _FILE_BYTE_LIMIT:
        return _fail("文件过大")
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return _fail("不是文本文件")


def read_text(root: Path, raw: object) -> dict:
    located = _locate(root, raw)
    if isinstance(located, dict):
        return located
    if not located.exists():
        return _fail("文件不存在")
    if located.is_dir():
        return _fail("这是目录")
    loaded = _read_utf8(located)
    if isinstance(loaded, dict):
        return loaded
    if len(loaded) > READ_LIMIT:
        loaded = f"{loaded[:READ_LIMIT]}\n...[已截断，共 {len(loaded)} 个字符]"
    return _ok(loaded)


def write_text(root: Path, raw: object, content: object) -> dict:
    if not isinstance(content, str):
        return _fail("缺少内容")
    located = _locate(root, raw)
    if isinstance(located, dict):
        return located
    if located.exists() and located.is_dir():
        return _fail("这是目录")
    before = ""
    if located.is_file():
        loaded = _read_utf8(located)
        before = loaded if isinstance(loaded, str) else ""
    located.parent.mkdir(parents=True, exist_ok=True)
    located.write_text(content, encoding="utf-8")
    shown = raw.strip() if isinstance(raw, str) else ""
    return _ok(f"已写入 {shown}", before=before, after=content)


def edit_text(root: Path, raw: object, old: object, new: object) -> dict:
    if not isinstance(old, str) or not old:
        return _fail("要替换的文本为空")
    if not isinstance(new, str):
        return _fail("缺少内容")
    located = _locate(root, raw)
    if isinstance(located, dict):
        return located
    if not located.exists() or not located.is_file():
        return _fail("文件不存在")
    loaded = _read_utf8(located)
    if isinstance(loaded, dict):
        return loaded
    count = loaded.count(old)
    if count == 0:
        return _fail("找不到要替换的文本")
    if count > 1:
        return _fail("要替换的文本出现了多次")
    updated = loaded.replace(old, new, 1)
    located.write_text(updated, encoding="utf-8")
    shown = raw.strip() if isinstance(raw, str) else ""
    return _ok(f"已修改 {shown}", before=loaded, after=updated)


def _inside_file(root: Path, path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def glob_files(root: Path, pattern: object) -> dict:
    if not isinstance(pattern, str) or not pattern.strip():
        return _fail("缺少模式")
    text = pattern.strip()
    if text.startswith("/") or ".." in Path(text).parts:
        return _fail("路径越界")
    root.mkdir(parents=True, exist_ok=True)
    found: list[str] = []
    for path in root.glob(text):
        if not _inside_file(root, path):
            continue
        found.append(path.relative_to(root).as_posix())
        if len(found) >= LIST_LIMIT:
            break
    found.sort()
    if not found:
        return _ok("没有匹配")
    body = "\n".join(found)
    if len(found) >= LIST_LIMIT:
        body += "\n已达到 200 条"
    return _ok(body)


def _search_roots(root: Path, raw: object) -> list[Path] | dict:
    if raw is None or raw == "":
        return [root]
    located = _locate(root, raw)
    if isinstance(located, dict):
        return located
    if not located.exists():
        return _fail("文件不存在")
    return [located]


def grep_text(root: Path, pattern: object, raw_path: object = None) -> dict:
    if not isinstance(pattern, str) or not pattern:
        return _fail("缺少正则")
    try:
        regex = re.compile(pattern)
    except re.error:
        return _fail("正则无法解析")
    roots = _search_roots(root, raw_path)
    if isinstance(roots, dict):
        return roots
    root.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for start in roots:
        files = [start] if start.is_file() else [path for path in start.rglob("*") if path.is_file()]
        for path in files:
            if not _inside_file(root, path):
                continue
            loaded = _read_utf8(path)
            if isinstance(loaded, dict):
                continue
            relative = path.relative_to(root).as_posix()
            for number, line in enumerate(loaded.splitlines(), start=1):
                if regex.search(line):
                    lines.append(f"{relative}:{number}:{line}")
                    if len(lines) >= LIST_LIMIT:
                        return _ok("\n".join(lines) + "\n已达到 200 条")
    if not lines:
        return _ok("没有匹配")
    return _ok("\n".join(lines))
