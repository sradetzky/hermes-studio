from __future__ import annotations

import mimetypes
import os
from email.utils import formatdate

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from webapp.safe_files import OpenedRegularFile


_CHUNK_SIZE = 1024 * 1024


class DescriptorFileResponse(Response):
    """Range-capable ASGI response bound to one already-open regular file."""

    def __init__(self, opened: OpenedRegularFile):
        super().__init__(content=b"")
        self._descriptor = os.dup(opened.descriptor)
        self._stat = opened.stat
        self._filename = opened.name

    def _range(self, value: str | None) -> tuple[int, int, int]:
        size = self._stat.st_size
        if not value:
            return 0, size - 1, 200
        if not value.startswith("bytes=") or "," in value:
            raise ValueError("unsupported range")
        start_text, separator, end_text = value[6:].partition("-")
        if not separator:
            raise ValueError("invalid range")
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError("invalid suffix range")
            start = max(0, size - suffix)
            end = size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError("unsatisfiable range")
        return start, min(end, size - 1), 206

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        size = self._stat.st_size
        try:
            try:
                start, end, status = self._range(Headers(scope=scope).get("range"))
            except (TypeError, ValueError):
                await send({
                    "type": "http.response.start",
                    "status": 416,
                    "headers": [
                        (b"accept-ranges", b"bytes"),
                        (b"content-range", f"bytes */{size}".encode("ascii")),
                        (b"content-length", b"0"),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})
                return

            length = 0 if size == 0 else end - start + 1
            media_type = mimetypes.guess_type(self._filename)[0] or "application/octet-stream"
            etag = f'"{self._stat.st_mtime_ns:x}-{size:x}"'
            headers = [
                (b"accept-ranges", b"bytes"),
                (b"content-length", str(length).encode("ascii")),
                (b"content-type", media_type.encode("latin-1")),
                (b"etag", etag.encode("ascii")),
                (b"last-modified", formatdate(
                    self._stat.st_mtime, usegmt=True).encode("ascii")),
            ]
            if status == 206:
                headers.append((
                    b"content-range",
                    f"bytes {start}-{end}/{size}".encode("ascii"),
                ))
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            })
            if scope["method"] == "HEAD" or length == 0:
                await send({"type": "http.response.body", "body": b""})
                return

            offset = start
            remaining = length
            while remaining:
                amount = min(_CHUNK_SIZE, remaining)
                chunk = await run_in_threadpool(
                    lambda: os.pread(self._descriptor, amount, offset))
                if not chunk:
                    raise OSError("opened media file ended before its retained size")
                offset += len(chunk)
                remaining -= len(chunk)
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": remaining > 0,
                })
        finally:
            os.close(self._descriptor)
