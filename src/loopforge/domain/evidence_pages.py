"""Lossless UTF-8 evidence paging with byte cursors, independent of providers."""


def evidence_page(
    raw: str, *, offset: int = 0, start_line: int = 1, line_count: int = 80, limit: int = 2000
) -> tuple[str, int | None]:
    body = raw.encode()
    if not offset:
        offset = sum(len(line) for line in body.splitlines(keepends=True)[: start_line - 1])
    if not 0 <= offset <= len(body):
        msg = "Evidence cursor is outside the artifact"
        raise ValueError(msg)
    try:
        remaining = body[offset:].decode()
    except UnicodeDecodeError:
        msg = "Evidence cursor must align with UTF-8"
        raise ValueError(msg) from None
    selected = "".join(remaining.splitlines(keepends=True)[:line_count]).encode()[:limit]
    page = selected.decode("utf-8", errors="ignore")
    end = offset + len(page.encode())
    return page, end if end < len(body) else None
