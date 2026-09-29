"""放行规则的前缀匹配。"""


def shell_prefix(command: str) -> str | None:
    parts = command.strip().split()
    if not parts:
        return None
    return " ".join(parts[:2])


def matches_prefix(text: str, prefix: str) -> bool:
    value = text.strip()
    pattern = prefix.strip()
    if not value or not pattern:
        return False
    return value == pattern or value.startswith(pattern + " ")


def shell_allowed(command: str, prefixes: list[str]) -> bool:
    return any(matches_prefix(command, prefix) for prefix in prefixes)


def path_allowed(path: str, patterns: list[str]) -> bool:
    return matched_pattern(path, patterns, spaced=False) is not None


def matched_pattern(text: str, patterns: list[str], *, spaced: bool) -> str | None:
    value = text.strip()
    if not value:
        return None
    for pattern in patterns:
        prefix = pattern.strip()
        if not prefix:
            continue
        if value == prefix:
            return prefix
        if spaced and value.startswith(prefix + " "):
            return prefix
        if not spaced and value.startswith(prefix):
            return prefix
    return None
