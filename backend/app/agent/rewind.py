"""把写入和替换按改前内容写回去。失败时恢复到回退前的文件。"""

from dataclasses import dataclass
from pathlib import Path

from app.tools.files import PathRejected, resolve_inside


class RewindError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class RevertStep:
    path: str
    existed: bool
    before: str


def apply_reverts(root: Path, steps: list[RevertStep]) -> tuple[list[str], dict[Path, str | None]]:
    resolved: list[tuple[RevertStep, Path]] = []
    for step in steps:
        try:
            path = resolve_inside(root, step.path)
        except PathRejected as exc:
            raise RewindError(f"写回失败：{step.path}。消息未删除") from exc
        resolved.append((step, path))

    snapshots: dict[Path, str | None] = {}
    for _, path in resolved:
        if path in snapshots:
            continue
        if path.is_file():
            try:
                snapshots[path] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise RewindError(f"写回失败：{_show(root, path)}。消息未删除") from exc
        elif path.exists():
            raise RewindError(f"写回失败：{_show(root, path)}。消息未删除")
        else:
            snapshots[path] = None

    restored: list[str] = []
    try:
        for step, path in resolved:
            if step.existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(step.before, encoding="utf-8")
            elif path.is_file():
                path.unlink()
            elif path.exists():
                raise OSError(path)
            shown = _show(root, path)
            if shown not in restored:
                restored.append(shown)
    except Exception as exc:
        restore_snapshots(snapshots)
        if isinstance(exc, RewindError):
            raise
        failed = ""
        if isinstance(exc, OSError) and isinstance(exc.filename, str):
            failed = exc.filename
        label = _show(root, Path(failed)) if failed else "文件"
        raise RewindError(f"写回失败：{label}。消息未删除") from exc
    return restored, snapshots


def _show(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name or str(path)


def restore_snapshots(snapshots: dict[Path, str | None]) -> None:
    for path, content in snapshots.items():
        if content is None:
            if path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
