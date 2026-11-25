"""Resumable, checksum-verified fetcher for the 14-part Lean 4 archive (and companions).

* Every part's sha256 and size, the joined archive's sha256 and the total size are pinned
  below (from the mirror's published `manifest.json`). The remote manifest is fetched and
  must match exactly.
* Downloads stream to `*.partial` files and resume with HTTP Range. Each file is verified
  (size, then sha256) and renamed into place atomically.
* Extraction streams the concatenated parts through gzip+tar while hashing them against
  the joined sha256. It allows only regular files and directories (`filter="data"`, no
  links, no absolute or `..` paths), caps total size and count, and lands atomically.

Everything lands in the git-ignored `data/raw/`. Nothing here is ever committed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

HF_REPO = "liu9178/riemann-lean-archive"
HF_RESOLVE = "https://huggingface.co/datasets/" + HF_REPO + "/resolve/{rev}/{path}"
HF_API = "https://huggingface.co/api/datasets/" + HF_REPO
ALPO_PART = "https://alpo.ge/{name}"
DEFAULT_PDFS = ("https://alpo.ge/remark.pdf",)
P0_FILES = ("p0/spine.tsv.gz", "p0/stats.json", "p0/report.md")
USER_AGENT = "eddie-fetch/0.1"

JOINED_SHA256 = "9631b4f020199e580ba86178cb56c6e9b09f690c2369a80dc4334aa93f49c7fc"
TOTAL_BYTES = 239_275_554
EXPECTED_LEAN_FILES = 175_187
PART_SIZE = 18_000_000
PARTS: dict[str, tuple[str, int]] = {
    "lean.tar.gz.part01": ("86974b1c242ca7b6748f3299df4fc64ee0f6e06c776ac964fb64888101a3de77", PART_SIZE),
    "lean.tar.gz.part02": ("1c2a2477354a9dfa97ff1ce54861e96ed885bd456fffbaaabc2028314a229c42", PART_SIZE),
    "lean.tar.gz.part03": ("1465a23b1b579a881e24e5ffaf7c4c3727af28ea24741f1ec80e1d67947530a3", PART_SIZE),
    "lean.tar.gz.part04": ("3929e327eecb4e2c5c7cae30e104e6a4a52fa6b82d28325bcda8e9da1c86893b", PART_SIZE),
    "lean.tar.gz.part05": ("f25d7e80f7c4ad6d674aa18b323ca760f848857c8755501c149317c38a66c75d", PART_SIZE),
    "lean.tar.gz.part06": ("a8c651a68327811b37dce3cca8eda5e0b2102fa227c0d395cbb7553e4dd69939", PART_SIZE),
    "lean.tar.gz.part07": ("65c7e502a9b236a0f68d9e0aa6e089d4c09b9fc7a0960a149384063ccbe5e869", PART_SIZE),
    "lean.tar.gz.part08": ("71b027ad6e4179c3a50ce72921e3fec0a2dd751ec7472ac2c644ad145a50ec7e", PART_SIZE),
    "lean.tar.gz.part09": ("47718977ebaa8613f1647e93134479780db9320c5fa911463fd312a6257b796f", PART_SIZE),
    "lean.tar.gz.part10": ("1d85ece78e0779cec26b0cce29c6462d9f90180e56d78120b152df101715240b", PART_SIZE),
    "lean.tar.gz.part11": ("cf83a159d1a9540012229c376562587dd966da10cd657cbcef53acfd03232448", PART_SIZE),
    "lean.tar.gz.part12": ("1aa5415bd2db83b2689afeeb946419303b3c4f599ae790ad74d38465f768b502", PART_SIZE),
    "lean.tar.gz.part13": ("2de4edbce8ebdbfd0b94bc89bab518c7294209f408ec5b14a7be9e4832d9da81", PART_SIZE),
    "lean.tar.gz.part14": ("53f8ea43a9b5dcab97b84a52f3ac2848711c844a5420a4c56418093bacb3c6f6", 5_275_554),
}
assert sum(size for _, size in PARTS.values()) == TOTAL_BYTES

MAX_EXTRACT_BYTES = 4 * 1024**3
MAX_EXTRACT_FILES = 400_000
CHUNK = 1 << 20


class FetchError(RuntimeError):
    pass


class ChecksumError(FetchError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --- downloading -----------------------------------------------------------------------------


async def download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    sha256: str | None = None,
    size: int | None = None,
    retries: int = 3,
    log=print,
) -> Path:
    """Resumable download with size + sha256 verification and an atomic rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (size is None or dest.stat().st_size == size) and (
        sha256 is None or await asyncio.to_thread(sha256_file, dest) == sha256
    ):
        return dest
    partial = dest.with_name(dest.name + ".partial")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        offset = partial.stat().st_size if partial.exists() else 0
        if size is not None and offset > size:
            partial.unlink()
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 416 and size is not None and offset == size:
                    pass  # already complete
                elif resp.status_code == 206 and offset:
                    with open(partial, "ab") as fh:
                        async for chunk in resp.aiter_bytes():  # unbuffered: progress survives a cut
                            fh.write(chunk)
                elif resp.status_code == 200:
                    with open(partial, "wb") as fh:  # server ignored Range: start over
                        async for chunk in resp.aiter_bytes():
                            fh.write(chunk)
                else:
                    raise FetchError(f"GET {url}: HTTP {resp.status_code}")
        except (httpx.TransportError, FetchError) as exc:
            last_error = exc
            log(f"  {dest.name}: attempt {attempt} interrupted ({type(exc).__name__}); resuming")
            await asyncio.sleep(min(2**attempt, 10) * 0.1)
            continue
        got = partial.stat().st_size if partial.exists() else 0
        if size is not None and got != size:
            last_error = FetchError(f"{dest.name}: got {got} bytes, expected {size}")
            if got > size:
                partial.unlink()
            continue
        if sha256 is not None:
            digest = await asyncio.to_thread(sha256_file, partial)
            if digest != sha256:
                partial.unlink()
                raise ChecksumError(f"{dest.name}: sha256 {digest} != pinned {sha256}")
        os.replace(partial, dest)
        return dest
    raise FetchError(f"{dest.name}: download failed after {retries} attempts: {last_error}")


async def resolve_revision(client: httpx.AsyncClient) -> str:
    resp = await client.get(HF_API)
    resp.raise_for_status()
    return resp.json()["sha"]


async def check_remote_manifest(client: httpx.AsyncClient, rev: str) -> None:
    resp = await client.get(HF_RESOLVE.format(rev=rev, path="manifest.json"))
    resp.raise_for_status()
    remote = resp.json()
    pinned = {name: sha for name, (sha, _) in PARTS.items()}
    if remote.get("archive_sha256") != JOINED_SHA256 or remote.get("parts") != pinned or remote.get("total_bytes") != TOTAL_BYTES:
        raise FetchError("remote manifest.json differs from the pinned hashes; refusing to continue")


async def hf_tree(client: httpx.AsyncClient, rev: str, folder: str) -> dict[str, dict]:
    resp = await client.get(f"{HF_API}/tree/{rev}/{folder}")
    resp.raise_for_status()
    return {entry["path"]: entry for entry in resp.json() if entry.get("type") == "file"}


async def fetch_parts(client: httpx.AsyncClient, rev: str, parts_dir: Path, *, jobs: int = 4, source: str = "hf", log=print) -> list[Path]:
    sem = asyncio.Semaphore(jobs)

    async def one(name: str, sha: str, size: int) -> Path:
        async with sem:
            primary = HF_RESOLVE.format(rev=rev, path=f"parts/{name}") if source == "hf" else ALPO_PART.format(name=name)
            try:
                path = await download(client, primary, parts_dir / name, sha256=sha, size=size, log=log)
            except ChecksumError:
                raise
            except FetchError:
                if source != "hf":
                    raise
                log(f"  {name}: HF failed, trying the original alpo.ge source")
                path = await download(client, ALPO_PART.format(name=name), parts_dir / name, sha256=sha, size=size, log=log)
            log(f"  {name}: ok ({size / 1e6:.1f} MB, sha256 verified)")
            return path

    return list(await asyncio.gather(*(one(n, s, z) for n, (s, z) in PARTS.items())))


# --- extraction ----------------------------------------------------------------------------------


class ConcatReader:
    """Read several files as one stream while hashing every byte."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self._paths = list(paths)
        self._index = 0
        self._fh = None
        self.hasher = hashlib.sha256()
        self.bytes_read = 0

    def read(self, n: int = -1) -> bytes:
        out = bytearray()
        while n < 0 or len(out) < n:
            if self._fh is None:
                if self._index >= len(self._paths):
                    break
                self._fh = open(self._paths[self._index], "rb")
                self._index += 1
            chunk = self._fh.read(CHUNK if n < 0 else n - len(out))
            if not chunk:
                self._fh.close()
                self._fh = None
                continue
            out += chunk
        data = bytes(out)
        self.hasher.update(data)
        self.bytes_read += len(data)
        return data

    def drain(self) -> None:
        while self.read(CHUNK):
            pass

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


@dataclass
class ExtractReport:
    files: int
    lean_files: int
    bytes: int
    joined_sha256: str
    top_level: list[str]


def extract_parts(
    parts: list[Path],
    dest: Path,
    *,
    expected_sha256: str | None,
    expected_lean_files: int | None = None,
    max_bytes: int = MAX_EXTRACT_BYTES,
    max_files: int = MAX_EXTRACT_FILES,
) -> ExtractReport:
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    reader = ConcatReader(parts)
    files = lean = total = 0
    top: set[str] = set()
    try:
        with tarfile.open(fileobj=reader, mode="r|gz") as tar:
            for member in tar:
                if not (member.isfile() or member.isdir()):
                    raise FetchError(f"refusing non-regular archive member {member.name!r} ({member.type!r})")
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise FetchError(f"refusing unsafe archive path {member.name!r}")
                safe = tarfile.data_filter(member, str(tmp))  # rejects absolute / '..' paths
                if member.isfile():
                    files += 1
                    total += member.size
                    if member.name.endswith(".lean"):
                        lean += 1
                    if files > max_files or total > max_bytes:
                        raise FetchError("archive exceeds the extraction size/count limits")
                top.add(member.name.split("/", 1)[0])
                tar.extract(safe, str(tmp), filter="data")
        reader.drain()
        digest = reader.hasher.hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ChecksumError(f"joined archive sha256 {digest} != pinned {expected_sha256}")
        if expected_lean_files is not None and lean != expected_lean_files:
            raise FetchError(f"extracted {lean} .lean files, expected {expected_lean_files}")
    except BaseException:
        reader.close()
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    reader.close()
    report = ExtractReport(files, lean, total, digest, sorted(top))
    (tmp / ".complete").write_text(json.dumps(report.__dict__, indent=2))
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    return report


# --- manifest + CLI ------------------------------------------------------------------------------


def _write_manifest(raw: Path, update: dict) -> None:
    path = raw / "FETCH_MANIFEST.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(update)
    data["updated_at"] = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


async def _run(args: argparse.Namespace) -> int:
    raw = Path(args.dest)
    raw.mkdir(parents=True, exist_ok=True)

    if args.sample:
        from eddie.ingest.synthetic import write_sample_tree

        out = Path(args.sample_dir)
        info = write_sample_tree(out / "tree", scale=args.scale)
        print(f"synthetic sample tree written to {out / 'tree'} ({info['lean_files']} .lean files)")
        return 0

    parts_dir = raw / "parts"
    if args.verify_only:
        bad = [n for n, (sha, size) in PARTS.items() if not (parts_dir / n).exists() or (parts_dir / n).stat().st_size != size or sha256_file(parts_dir / n) != sha]
        print("all 14 parts verified" if not bad else f"missing or corrupt parts: {bad}")
        return 0 if not bad else 1

    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(120.0, connect=20.0)) as client:
        rev = args.revision or await resolve_revision(client)
        print(f"mirror revision {rev}")
        await check_remote_manifest(client, rev)
        tree = await hf_tree(client, rev, "parts")
        for name, (sha, size) in PARTS.items():
            lfs = tree.get(f"parts/{name}", {}).get("lfs") or {}
            if lfs and (lfs.get("oid") != sha or lfs.get("size") != size):
                raise FetchError(f"HF LFS metadata for {name} disagrees with the pinned hash")
        print("downloading 14 parts (resumable)…")
        paths = await fetch_parts(client, rev, parts_dir, jobs=args.jobs, source=args.source)
        manifest: dict = {"revision": rev, "parts": {n: {"sha256": s, "size": z} for n, (s, z) in PARTS.items()}}

        if args.p0:
            entries = await hf_tree(client, rev, "p0")
            p0 = {}
            for rel in P0_FILES:
                entry = entries.get(rel, {})
                lfs = entry.get("lfs") or {}
                target = raw / rel
                await download(client, HF_RESOLVE.format(rev=rev, path=rel), target, sha256=lfs.get("oid"), size=entry.get("size"))
                if not lfs and entry.get("oid") and git_blob_sha1(target.read_bytes()) != entry["oid"]:
                    target.unlink()
                    raise ChecksumError(f"{rel}: git blob hash mismatch")
                p0[rel] = sha256_file(target)
                print(f"  {rel}: ok")
            manifest["p0"] = p0

        if args.pdf or args.pdf_url:
            known = json.loads((raw / "FETCH_MANIFEST.json").read_text()).get("pdfs", {}) if (raw / "FETCH_MANIFEST.json").exists() else {}
            pdfs = {}
            for url in args.pdf_url or DEFAULT_PDFS:
                target = raw / "pdfs" / url.rsplit("/", 1)[-1]
                await download(client, url, target)
                digest = sha256_file(target)
                if url in known and known[url] != digest:
                    print(f"  WARNING: {url} changed since first download (was {known[url]})")
                pdfs[url] = digest
                print(f"  {target.name}: {target.stat().st_size / 1e3:.0f} KB, sha256 {digest[:12]}… (trust on first use)")
            manifest["pdfs"] = pdfs
    _write_manifest(raw, manifest)

    if not args.no_extract:
        dest = raw / "extracted"
        if (dest / ".complete").exists() and not args.force_extract:
            print(f"already extracted at {dest} (use --force-extract to redo)")
        else:
            print("verifying joined sha256 while extracting (streamed, links refused)…")
            report = extract_parts(paths, dest, expected_sha256=JOINED_SHA256, expected_lean_files=EXPECTED_LEAN_FILES)
            _write_manifest(raw, {"joined_sha256": report.joined_sha256, "extracted": report.__dict__})
            print(f"extracted {report.lean_files} .lean files ({report.bytes / 1e9:.2f} GB) to {dest}; top level: {report.top_level}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch the Lean 4 RH archive (resumable, sha256-verified)")
    p.add_argument("--dest", default=str(Path(os.environ.get("EDDIE_DATA_DIR", "data")) / "raw"))
    p.add_argument("--sample", action="store_true", help="write the synthetic mini tree instead (instant, offline)")
    p.add_argument("--sample-dir", default=str(Path(os.environ.get("EDDIE_DATA_DIR", "data")) / "sample"))
    p.add_argument("--scale", type=int, default=0, help="extra generated modules in the sample tree")
    p.add_argument("--p0", action="store_true", help="also fetch p0/spine.tsv.gz, stats.json, report.md")
    p.add_argument("--pdf", action="store_true", help="also fetch companion PDFs (remark.pdf)")
    p.add_argument("--pdf-url", action="append", help="companion PDF URL (repeatable)")
    p.add_argument("--source", choices=["hf", "alpo"], default="hf")
    p.add_argument("--revision", help="pin a mirror revision (default: resolve current)")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--force-extract", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except FetchError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1
