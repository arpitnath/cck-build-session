#!/usr/bin/env python3
"""
JSONL → terminal-style transcript renderer.

Mimics the actual Claude Code terminal output. Less markdown, more "what
the developer actually saw on screen."

- User prompts:    >  prefix
- Assistant text:  ⏺  prefix
- Tool calls:      ⏺  ToolName(brief args)
- Tool results:    ⎿  indented under tool call
- Thinking:        ✻  italic-style block (preserved, not collapsed)
- Hooks:           [hook] marker, kept inline (no <details>)
- Pure metadata entries: skipped

Verbatim content. Only structural reformatting.
"""

import json
import sys
import re

SKIP_TYPES = {
    "permission-mode",
    "ai-title",
    "last-prompt",
    "direct",
    "file-history-snapshot",
}

TRUNCATE_TOOL_RESULT_AT = 3000


def short_tool_args(name, tool_input):
    """One-line representation of a tool's input — what shows in the terminal header."""
    if not isinstance(tool_input, dict):
        return ""
    if name == "Bash":
        cmd = tool_input.get("command", "")
        # Show first ~120 chars
        if len(cmd) > 120:
            cmd = cmd[:120] + "..."
        return cmd
    if name in {"Read", "Glob", "Grep"}:
        path = tool_input.get("file_path") or tool_input.get("pattern") or tool_input.get("path") or ""
        return path
    if name in {"Edit", "Write", "MultiEdit"}:
        return tool_input.get("file_path", "")
    if name == "Task":
        return tool_input.get("description", "")[:80]
    if name == "TodoWrite":
        todos = tool_input.get("todos", [])
        return f"{len(todos)} items"
    # Generic — first short field
    for k, v in tool_input.items():
        if isinstance(v, str) and len(v) < 100:
            return f"{k}={v}"
    return ""


def indent_lines(text, prefix="     "):
    """Indent every line with the given prefix."""
    return "\n".join(prefix + ln for ln in text.splitlines())


def render_content_blocks(blocks):
    """Render a list of content blocks (text, tool_use, tool_result, thinking)."""
    pieces = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "").strip()
            if text:
                pieces.append(("text", text))

        elif btype == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking:
                pieces.append(("thinking", thinking))

        elif btype == "tool_use":
            name = block.get("name", "tool")
            tool_input = block.get("input", {})
            args = short_tool_args(name, tool_input)
            full = json.dumps(tool_input, indent=2, ensure_ascii=False)
            pieces.append(("tool_use", name, args, full))

        elif btype == "tool_result":
            content = block.get("content", "")
            if isinstance(content, list):
                pcs = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        pcs.append(c.get("text", ""))
                content = "\n".join(pcs)
            if not isinstance(content, str):
                content = str(content)
            pieces.append(("tool_result", content))

    return pieces


def render_pieces(pieces):
    """Convert pieces to terminal-style text."""
    out = []
    for piece in pieces:
        kind = piece[0]

        if kind == "text":
            text = piece[1]
            # ⏺ prefix on first line, continuation indented
            lines = text.splitlines()
            if lines:
                out.append("⏺ " + lines[0])
                for ln in lines[1:]:
                    out.append("  " + ln)

        elif kind == "thinking":
            thinking = piece[1]
            out.append("✻ Thinking…")
            for ln in thinking.splitlines():
                out.append("  " + ln)

        elif kind == "tool_use":
            _, name, args, full = piece
            # Header captures the meaningful args. JSON dump suppressed —
            # real terminal output doesn't show the internal payload.
            header = f"⏺ {name}({args})" if args else f"⏺ {name}"
            out.append(header)

        elif kind == "tool_result":
            content = piece[1]
            if len(content) > TRUNCATE_TOOL_RESULT_AT:
                content = (
                    content[:TRUNCATE_TOOL_RESULT_AT]
                    + f"\n  ⋯ truncated, {len(content) - TRUNCATE_TOOL_RESULT_AT} more chars"
                )
            lines = content.splitlines()
            if lines:
                out.append("  ⎿ " + lines[0])
                for ln in lines[1:]:
                    out.append("    " + ln)

    return "\n".join(out)


# Markers that identify auto-injected skill content (not the user's actual work)
SKILL_CONTENT_MARKERS = (
    "superpowers:using-superpowers",
    "using-superpowers",
    "<EXTREMELY_IMPORTANT>",
    "<SUBAGENT-STOP>",
    "Deep Context Builder",
    "# Deep Context",
    "Base directory for this skill",
    "skill - your introduction to using skills",
)


def is_skill_content(text):
    """Skill prompts are auto-injected, not part of the actual work — strip them."""
    if not isinstance(text, str):
        return False
    return any(marker in text for marker in SKILL_CONTENT_MARKERS)


def render_hook(entry):
    attachment = entry.get("attachment", {})
    hook_name = attachment.get("hookName", "hook")
    stdout = attachment.get("stdout", "")
    if not stdout.strip():
        return None
    # Extract additionalContext if it's a wrapped hook response
    try:
        parsed = json.loads(stdout)
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        if ctx:
            stdout = ctx
    except (json.JSONDecodeError, AttributeError):
        pass

    # Skip skill-content hook injections (superpowers, deep-context, etc.)
    if is_skill_content(stdout):
        return None

    if len(stdout) > 1500:
        stdout = stdout[:1500] + "\n⋯ truncated"

    out = [f"[hook: {hook_name}]"]
    for ln in stdout.splitlines():
        out.append("  " + ln)
    return "\n".join(out)


def render_user_text(content):
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                pieces.append(block.get("text", ""))
            elif isinstance(block, str):
                pieces.append(block)
        content = "\n".join(pieces)
    if not isinstance(content, str):
        return None
    content = content.strip()
    if not content:
        return None
    # Skip pure metadata wrappers (command invocations, local stdout, etc.)
    if content.startswith("<command-message>") or content.startswith("<command-name>"):
        return None
    if content.startswith("<local-command-stdout>"):
        return None
    if content.startswith("<bash-input>") or content.startswith("<bash-stdout>") or content.startswith("<bash-stderr>"):
        # These are bash-wrapped command results from claude1 itself — skip the raw wrappers
        return None
    # Skip skill auto-injections (deep-context skill content etc.)
    if is_skill_content(content):
        return None
    lines = content.splitlines()
    out = ["> " + lines[0]]
    for ln in lines[1:]:
        out.append("  " + ln)
    return "\n".join(out)


def render_entry(entry):
    etype = entry.get("type")
    if etype in SKIP_TYPES:
        return None
    if etype == "system":
        text = entry.get("content", "")
        if isinstance(text, str) and text.strip():
            return f"[system] {text.strip()[:300]}"
        return None
    if etype == "attachment":
        attachment = entry.get("attachment", {})
        if attachment.get("type") == "hook_success":
            return render_hook(entry)
        return None

    if etype in {"user", "assistant", "message"}:
        msg = entry.get("message", entry)
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            return render_user_text(content)

        if role == "assistant":
            if isinstance(content, str):
                lines = content.strip().splitlines()
                if not lines:
                    return None
                out = ["⏺ " + lines[0]]
                for ln in lines[1:]:
                    out.append("  " + ln)
                return "\n".join(out)
            if isinstance(content, list):
                pieces = render_content_blocks(content)
                rendered = render_pieces(pieces)
                return rendered if rendered.strip() else None

    return None


def main(jsonl_path):
    with open(jsonl_path, "r") as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        rendered = render_entry(entry)
        if rendered:
            print(rendered)
            print()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "cck-build-session.jsonl"
    main(path)
