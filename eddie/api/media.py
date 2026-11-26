"""Read-only routes for the demo film: metadata, the MP4 (with byte ranges) and its poster.

Starlette's FileResponse answers `Range: bytes=...` with `206 Partial Content`, so a browser can
scrub the film without downloading all of it. Filenames are fixed: no path comes from a request.
Behind Basic Auth nothing here is `public` (a shared cache must not keep it); after the unlock,
`EDDIE_PUBLIC_CACHE=1` lets Cloudflare cache the poster and the film.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from eddie import media


def cache_header(public_max_age: int) -> str:
    if os.environ.get("EDDIE_PUBLIC_CACHE") == "1":
        return f"public, max-age={public_max_age}"
    return "no-cache"


def register(router: APIRouter) -> None:
    @router.get("/demo-video/meta")
    def demo_video_meta() -> dict:
        return media.meta()

    @router.get("/demo-video")
    def demo_video() -> FileResponse:
        directory = media.media_dir()
        if not media.serves_local_file(directory):
            raise HTTPException(404, "the demo film is not served from here")
        return FileResponse(directory / media.VIDEO_NAME, media_type="video/mp4",
                            headers={"Cache-Control": cache_header(3600)})

    @router.get("/demo-video/poster")
    def demo_video_poster() -> FileResponse:
        path = media.media_dir() / media.POSTER_NAME
        if not path.is_file():
            raise HTTPException(404, "no poster")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": cache_header(3600)})
