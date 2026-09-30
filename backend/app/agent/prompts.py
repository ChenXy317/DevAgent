"""发给模型的系统提示。"""


def system_prompt(workdir: str, has_summary: bool) -> str:
    lines = [
        "你是一个在本机工作的编程助手。",
        f"当前会话的工作目录是 {workdir}。",
        "文件工具可以收相对路径，也可以收这个目录里的绝对路径。",
        "批准后的 shell 仍以当前系统用户执行。",
        "网页上的事实只引用 web_search 返回的片段。",
        "改文件和跑命令可能要等用户点头。",
        "做完调用 finish_task。",
    ]
    if has_summary:
        lines.append("总结里的决定和文件改动视为已经发生。")
    return "\n".join(lines)


def compression_prompt() -> str:
    return (
        "根据记录写一份能接着干活的纪要。"
        "覆盖：用户目标、已做决定、改过的文件、命令结果、未完成的事项、仍然有效的约束。"
        "不要写成对用户的寒暄。"
    )
