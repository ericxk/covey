"""Small read-only MCP server scoped to one task workspace (and the topic's received
attachments); never runs commands and never writes."""
from __future__ import annotations

import argparse
import itertools
import json
import os
from pathlib import Path
import stat
import sys


MAX_FILE = 4 * 1024 * 1024
MAX_OUTPUT = 48 * 1024
TOOLS = [
    {"name": "workspace_read_file", "description": "Read a UTF-8 text file in the current workspace, or an attachment received in this topic (its absolute path is given in the request), without shell commands. Line numbers start at 1; follow next_line for more content. Symlinks and files larger than 4 MiB are not supported.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": ["path"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "workspace_list_directory", "description": "List one directory in the current workspace without shell commands. Returns file names and types, with pagination. Does not follow symlinks or recurse.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["path"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
]


class WorkspaceReader:
    def __init__(self, root, extra_roots=()):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")
        # Read-only extra roots, such as the topic's incoming_media. One may not
        # exist yet when the server starts; it is checked on each read.
        self.extra_roots = [Path(extra).absolute() for extra in extra_roots]

    def tools(self) -> list:
        """What ``tools/list`` answers: the two reading tools."""
        return TOOLS

    def open(self, value, *, directory=False):
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("path must be a nonempty workspace path")
        path, base = Path(value), self.root
        if path.is_absolute():
            # Match the path as written or as resolved; always open the resolved root.
            matches = ((variant, candidate.resolve()) for candidate in (self.root, *self.extra_roots)
                       for variant in dict.fromkeys((candidate, candidate.resolve())))
            for variant, resolved in matches:
                try:
                    path, base = path.relative_to(variant), resolved
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("Path is outside the task workspace and this topic's attachments")
        if ".." in path.parts:
            raise ValueError("Parent traversal is not allowed")
        # Open each component relative to its parent fd, so symlink swaps cannot
        # redirect a read to another workspace (including between check/open).
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for i, part in enumerate(path.parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if i < len(path.parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                raise ValueError("Expected a directory" if directory else "Only regular text files can be read")
            return fd, str(path if base == self.root else base / path)
        except BaseException:
            os.close(fd)
            raise

    def call(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        spec = next((x for x in TOOLS if x["name"] == name), None)
        if spec is None or set(args) - set(spec["inputSchema"]["properties"]):
            raise ValueError("Unknown tool or argument")
        for key, default, low, high in (("start_line", 1, 1, MAX_FILE), ("offset", 0, 0, 10000),
                                      ("limit", 200, 1, 500 if name == "workspace_read_file" else 200)):
            value = args.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid {key}")
        fd, path = self.open(args.get("path"), directory=name == "workspace_list_directory")
        try:
            if name == "workspace_list_directory":
                with os.scandir(fd) as entries:
                    rows = [{"name": x.name, "type": "symlink" if x.is_symlink() else "directory" if x.is_dir(follow_symlinks=False) else "file" if x.is_file(follow_symlinks=False) else "special"}
                            for x in itertools.islice(entries, 10001)]
                if len(rows) > 10000:
                    raise ValueError("Directory has over 10,000 entries; choose a narrower path")
                rows.sort(key=lambda x: x["name"])
                start, limit = args.get("offset", 0), args.get("limit", 100)
                page, used = [], 0
                for row in rows[start:start + limit]:
                    size = len(json.dumps(row, ensure_ascii=False).encode())
                    if used + size > MAX_OUTPUT:
                        break
                    page.append(row)
                    used += size
                end = start + len(page)
                return {"path": path, "entries": page, "total": len(rows), "next_offset": end if end < len(rows) else None}
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise ValueError("File exceeds 4 MiB; use a smaller source file")
            if b"\0" in data:
                raise ValueError("Binary files are not supported")
            lines = data.decode("utf-8").splitlines()
            start, limit = args.get("start_line", 1) - 1, args.get("limit", 200)
            page, used = [], 0
            for number, line in enumerate(lines[start:start + limit], start + 1):
                row = f"{number}: {line}"
                size = len(json.dumps(row, ensure_ascii=False).encode())
                if used + size > MAX_OUTPUT:
                    if not page:
                        raise ValueError("Single line exceeds the output limit")
                    break
                page.append(row)
                used += size
            end = start + len(page)
            return {"path": path, "total_lines": len(lines), "content": "\n".join(page), "next_line": end + 1 if end < len(lines) else None}
        finally:
            os.close(fd)


def serve(root, source=sys.stdin, destination=sys.stdout, extra_roots=()):
    reader = WorkspaceReader(root, extra_roots)
    for line in source:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or "id" not in row:
            continue
        method, params = row.get("method"), row.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {"protocolVersion": requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2024-11-05",
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "bridge-workspace", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": reader.tools()}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            try:
                if not isinstance(params, dict):
                    raise ValueError("Tool parameters must be an object")
                output = reader.call(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}], "isError": False}
            except (ValueError, OSError) as exc:
                # OSError can contain absolute external paths; only publish its class.
                message = str(exc) if isinstance(exc, ValueError) else "File access failed (missing, symlink, or unreadable path)"
                result = {"content": [{"type": "text", "text": message}], "isError": True}
        else:
            destination.write(json.dumps({"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32601, "message": "Unsupported method"}}) + "\n")
            destination.flush()
            continue
        destination.write(json.dumps({"jsonrpc": "2.0", "id": row["id"], "result": result}, ensure_ascii=False) + "\n")
        destination.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="covey_workspace_reader.py", description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--extra-root", action="append", default=[], type=Path,
                        help="additional read-only directory, e.g. the topic's received attachments")
    args = parser.parse_args()
    serve(args.root, extra_roots=args.extra_root)
