"""Fetcher: resumable downloads, checksum aborts, safe streamed extraction, sample tree."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import httpx
import pytest

from eddie.ingest import fetch
from eddie.ingest.fetch import ChecksumError, FetchError, download, extract_parts
from eddie.ingest.synthetic import SAMPLE_FILES, write_sample_tree

pytestmark = pytest.mark.anyio


def _range_server(blob: bytes, *, cut_first_at: int | None = None, corrupt: bool = False):
    state = {"calls": 0, "ranges": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        rng = request.headers.get("range")
        state["ranges"].append(rng)
        data = bytearray(blob)
        if corrupt:
            data[0] ^= 0xFF
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return httpx.Response(416)
            return httpx.Response(206, content=bytes(data[start:]))
        if cut_first_at is not None and state["calls"] == 1:
            stream = _CutStream(bytes(data), cut_first_at)
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, content=bytes(data))

    return handler, state


class _CutStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes, cut: int) -> None:
        self.data, self.cut = data, cut

    async def __aiter__(self):
        yield self.data[: self.cut]
        raise httpx.ReadError("connection dropped")


async def test_download_resumes_after_a_cut(tmp_path):
    blob = bytes(range(256)) * 400
    handler, state = _range_server(blob, cut_first_at=10_000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        path = await download(client, "https://mirror.invalid/part", tmp_path / "part", sha256=hashlib.sha256(blob).hexdigest(), size=len(blob), log=lambda *_: None)
    assert path.read_bytes() == blob
    assert state["ranges"][0] is None and state["ranges"][1] == "bytes=10000-"
    assert not (tmp_path / "part.partial").exists()


async def test_checksum_mismatch_aborts_and_deletes_partial(tmp_path):
    blob = b"x" * 5_000
    handler, _ = _range_server(blob, corrupt=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChecksumError):
            await download(client, "https://mirror.invalid/part", tmp_path / "part", sha256=hashlib.sha256(blob).hexdigest(), size=len(blob), log=lambda *_: None)
    assert not (tmp_path / "part").exists()
    assert not (tmp_path / "part.partial").exists()


async def test_verified_file_is_not_downloaded_again(tmp_path):
    blob = b"abc" * 100
    target = tmp_path / "part"
    target.write_bytes(blob)
    handler, state = _range_server(blob)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await download(client, "https://mirror.invalid/part", target, sha256=hashlib.sha256(blob).hexdigest(), size=len(blob))
    assert state["calls"] == 0


def _tar_gz(members: list[tuple[str, bytes | None, str]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
    return buf.getvalue()


def _split(blob: bytes, tmp: Path, n: int = 3) -> list[Path]:
    step = len(blob) // n + 1
    paths = []
    for i in range(n):
        p = tmp / f"lean.tar.gz.part{i + 1:02d}"
        p.write_bytes(blob[i * step : (i + 1) * step])
        paths.append(p)
    return paths


def test_streamed_extraction_verifies_joined_hash(tmp_path):
    blob = _tar_gz([("tree", None, "dir"), ("tree/src/Main.lean", b"theorem t : True := trivial\n", "file"), ("tree/src/A.lean", b"def a := 1\n", "file")])
    parts = _split(blob, tmp_path)
    report = extract_parts(parts, tmp_path / "extracted", expected_sha256=hashlib.sha256(blob).hexdigest(), expected_lean_files=2)
    assert report.lean_files == 2 and report.top_level == ["tree"]
    assert (tmp_path / "extracted" / "tree" / "src" / "Main.lean").exists()
    assert (tmp_path / "extracted" / ".complete").exists()


def test_joined_hash_mismatch_leaves_nothing(tmp_path):
    blob = _tar_gz([("tree/x.lean", b"x", "file")])
    with pytest.raises(ChecksumError):
        extract_parts(_split(blob, tmp_path), tmp_path / "extracted", expected_sha256="0" * 64)
    assert not (tmp_path / "extracted").exists()
    assert not (tmp_path / "extracted.tmp").exists()


@pytest.mark.parametrize(
    "members",
    [
        [("../evil.txt", b"pwned", "file")],
        [("/abs/evil.txt", b"pwned", "file")],
        [("tree/link", None, "symlink")],
    ],
)
def test_unsafe_members_are_refused(tmp_path, members):
    blob = _tar_gz(members)
    with pytest.raises((FetchError, tarfile.FilterError)):
        extract_parts(_split(blob, tmp_path, 1), tmp_path / "extracted", expected_sha256=None)
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (tmp_path / "extracted").exists()


def test_extraction_limits(tmp_path):
    blob = _tar_gz([(f"tree/{i}.lean", b"x" * 10, "file") for i in range(5)])
    with pytest.raises(FetchError, match="limits"):
        extract_parts(_split(blob, tmp_path, 1), tmp_path / "out", expected_sha256=None, max_files=3)


def test_pinned_manifest_is_self_consistent():
    assert len(fetch.PARTS) == 14
    assert sum(size for _, size in fetch.PARTS.values()) == fetch.TOTAL_BYTES == 239_275_554


def test_sample_tree(tmp_path):
    info = write_sample_tree(tmp_path / "tree", scale=3)
    assert (tmp_path / "tree" / "src" / "Main.lean").exists()
    assert (tmp_path / "tree" / "src" / "Development" / "Generated" / "M2.lean").exists()
    assert info["lean_files"] == len(SAMPLE_FILES) + 3 + 1


def test_cli_sample_mode(tmp_path, capsys):
    code = fetch.main(["--sample", "--sample-dir", str(tmp_path / "sample"), "--dest", str(tmp_path / "raw")])
    assert code == 0
    assert (tmp_path / "sample" / "tree" / "src" / "Statements.lean").exists()
