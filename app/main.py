"""OMR Compare: run Audiveris, oemer and homr on one score and compare the MusicXML."""
from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from lxml import etree

DATA = Path(os.environ.get("DATA_DIR", "/data"))
JOBS = DATA / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "40")) * 1024 * 1024
MAX_PAGES = int(os.environ.get("MAX_PAGES", "6"))
ENGINE_TIMEOUT = int(os.environ.get("ENGINE_TIMEOUT", "900"))
OEMER_TIMEOUT = int(os.environ.get("OEMER_TIMEOUT", "3000"))  # per page; oemer is very slow on CPU
KEEP_HOURS = float(os.environ.get("KEEP_HOURS", "72"))
STATIC = Path(__file__).parent / "static"

ALL_ENGINES = ["audiveris", "oemer", "homr"]
ENGINES = [e for e in os.environ.get("ENGINES", "audiveris,homr").split(",") if e in ALL_ENGINES]
ENGINE_INFO = {
    "audiveris": {"label": "Audiveris", "version": "5.11.0", "url": "https://github.com/Audiveris/audiveris"},
    "oemer": {"label": "oemer", "version": "0.1.8", "url": "https://github.com/BreezeWhite/oemer"},
    "homr": {"label": "homr", "version": "0.7.1", "url": "https://github.com/liebharc/homr"},
}

app = FastAPI(title="OMR Compare")
# Outputs are public, read-only, credential-free: let other sites (e.g. a MusicXML viewer) fetch them.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])
work_q: "queue.Queue[str]" = queue.Queue()
lock = threading.Lock()
pending: list[str] = []

# ---------------------------------------------------------------- job state

def job_dir(jid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", jid):
        raise HTTPException(404, "no such job")
    d = JOBS / jid
    if not d.is_dir():
        raise HTTPException(404, "no such job")
    return d


def load(jid: str) -> dict:
    return json.loads((JOBS / jid / "job.json").read_text())


def save(jid: str, job: dict) -> None:
    p = JOBS / jid / "job.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1))
    tmp.replace(p)


def update(jid: str, fn) -> dict:
    with lock:
        job = load(jid)
        fn(job)
        save(jid, job)
        return job

# ---------------------------------------------------------------- input handling

def sniff(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    return None


def check_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"can't resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(400, "that URL points at a private address")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_url(url: str) -> tuple[bytes, str]:
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(5):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise HTTPException(400, "only http(s) URLs are supported")
        check_public_host(u.hostname)
        req = urllib.request.Request(url, headers={"User-Agent": "omr-compare/0.1"})
        try:
            resp = opener.open(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise HTTPException(400, f"fetch failed: HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"fetch failed: {e}")
        data = resp.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "file too large")
        name = Path(u.path).name or "download"
        return data, name
    raise HTTPException(400, "too many redirects")


def prepare_pages(d: Path, src: Path, kind: str) -> list[str]:
    """Rasterise PDFs (for oemer and previews); returns relative page image paths."""
    pages = d / "pages"
    pages.mkdir(exist_ok=True)
    if kind == "pdf":
        subprocess.run(["pdftoppm", "-r", "300", "-png", "-l", str(MAX_PAGES), str(src), str(pages / "page")],
                       check=True, timeout=300)
        out = sorted(pages.glob("page*.png"))
    else:
        dst = pages / f"page-1.{kind}"
        shutil.copy(src, dst)
        out = [dst]
    return [str(p.relative_to(d)) for p in out]

# ---------------------------------------------------------------- engines

def run(cmd: list[str], log: Path, cwd: Path | None = None, timeout: int | None = None) -> int:
    with log.open("ab") as f:
        f.write(("$ " + " ".join(cmd) + "\n").encode())
        f.flush()
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=cwd, timeout=timeout or ENGINE_TIMEOUT,
                           env={**os.environ, "JAVA_TOOL_OPTIONS": "-Xmx2g -Djava.awt.headless=true"})
        f.write(f"\n[exit {p.returncode}]\n".encode())
        if p.returncode in (-9, 137):
            raise MemoryError("killed: ran out of memory (container is capped at 4 GB)")
        return p.returncode


def eng_audiveris(d: Path, out: Path, log: Path, job: dict) -> None:
    src = d / job["input"]
    run(["/opt/audiveris/bin/Audiveris", "-batch", "-export", "-output", str(out), "--", str(src)], log)


def eng_oemer(d: Path, out: Path, log: Path, job: dict) -> None:
    for i, page in enumerate(job["pages"], 1):
        pdir = out / f"p{i}"
        pdir.mkdir()
        run(["/opt/oemer/bin/oemer", str(d / page), "-o", str(pdir)], log, timeout=OEMER_TIMEOUT)
        for f in pdir.glob("*.musicxml"):
            f.rename(out / (f"page-{i}.musicxml" if len(job["pages"]) > 1 else f"{Path(job['input']).stem}.musicxml"))


def eng_homr(d: Path, out: Path, log: Path, job: dict) -> None:
    # homr reads PDFs itself, but give it our page images so the page cap matches oemer's.
    imgs = []
    for page in job["pages"]:
        dst = out / Path(page).name
        shutil.copy(d / page, dst)
        imgs.append(str(dst))
    run(["/opt/homr/bin/homr", *imgs], log, cwd=out)
    for p in imgs:
        Path(p).unlink(missing_ok=True)


RUNNERS = {"audiveris": eng_audiveris, "oemer": eng_oemer, "homr": eng_homr}
RESULT_EXT = (".mxl", ".musicxml", ".xml")


def collect(out: Path) -> list[str]:
    files = [p for p in out.rglob("*") if p.is_file() and p.suffix.lower() in RESULT_EXT]
    return sorted(str(p.relative_to(out)) for p in files)


def process(jid: str) -> None:
    d = JOBS / jid
    job = update(jid, lambda j: j.update(status="running", started=time.time()))
    try:
        if not job.get("pages"):
            pages = prepare_pages(d, d / job["input"], job["kind"])
            job = update(jid, lambda j: j.update(pages=pages))
    except Exception as e:  # noqa: BLE001
        update(jid, lambda j: j.update(status="error", error=f"could not read input: {e}"))
        return
    for eng in [e for e in ENGINES if e in job["engines"]]:
        if job["engines"][eng]["status"] in ("done", "failed"):
            continue
        out = d / "out" / eng
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        log = d / "out" / f"{eng}.log"
        log.unlink(missing_ok=True)
        t0 = time.time()
        update(jid, lambda j: j["engines"][eng].update(status="running", started=t0))
        err = None
        try:
            RUNNERS[eng](d, out, log, job)
        except subprocess.TimeoutExpired as e:
            err = f"timed out after {int(e.timeout)}s"
        except MemoryError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        files = collect(out)
        stats = {}
        for f in files:
            try:
                stats[f] = summarize(read_musicxml(out / f))
            except Exception as e:  # noqa: BLE001
                stats[f] = {"error": str(e)}
        status = "done" if files else "failed"
        if not files and not err:
            err = "no MusicXML produced (see log)"

        def fin(j, status=status, files=files, stats=stats, err=err):
            j["engines"][eng].update(status=status, seconds=round(time.time() - t0, 1),
                                     files=files, stats=stats, error=err)
        update(jid, fin)
    update(jid, lambda j: j.update(status="done", finished=time.time()))


def worker() -> None:
    while True:
        jid = work_q.get()
        try:
            process(jid)
        except Exception as e:  # noqa: BLE001
            try:
                update(jid, lambda j: j.update(status="error", error=str(e)))
            except Exception:  # noqa: BLE001
                pass
        finally:
            with lock:
                if jid in pending:
                    pending.remove(jid)


def janitor() -> None:
    while True:
        cutoff = time.time() - KEEP_HOURS * 3600
        for d in JOBS.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff and d.name not in pending:
                    shutil.rmtree(d)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(3600)

# ---------------------------------------------------------------- MusicXML helpers

def read_musicxml(path: Path) -> bytes:
    if path.suffix.lower() == ".mxl" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            root = None
            try:
                c = etree.fromstring(z.read("META-INF/container.xml"))
                rf = c.find(".//{*}rootfile")
                root = rf.get("full-path") if rf is not None else None
            except KeyError:
                pass
            if not root:
                root = next(n for n in z.namelist() if n.endswith((".xml", ".musicxml")) and not n.startswith("META-INF"))
            return z.read(root)
    return path.read_bytes()


def parse(data: bytes) -> etree._Element:
    parser = etree.XMLParser(remove_blank_text=True, remove_comments=False, resolve_entities=False,
                             no_network=True, load_dtd=False)
    return etree.fromstring(data, parser)


LAYOUT_TAGS = {"defaults", "credit", "print", "identification", "encoding", "appearance", "page-layout",
               "system-layout", "staff-layout", "scaling", "midi-device", "midi-instrument", "score-instrument",
               "supports", "software", "encoding-date"}
LAYOUT_ATTRS = {"default-x", "default-y", "relative-x", "relative-y", "width", "color", "font-family",
                "font-size", "font-style", "font-weight", "bezier-x", "bezier-y", "id", "print-object",
                "halign", "valign", "justify", "placement", "orientation", "dynamics"}


def normalize(data: bytes, mode: str) -> str:
    if mode == "raw":
        return data.decode("utf-8", "replace")
    root = parse(data)
    if mode == "semantic":
        for c in root.xpath("//comment()"):
            c.getparent().remove(c)
        for el in list(root.iter()):
            if not isinstance(el.tag, str):
                continue
            if etree.QName(el).localname in LAYOUT_TAGS and el.getparent() is not None:
                el.getparent().remove(el)
                continue
            for a in list(el.attrib):
                if a in LAYOUT_ATTRS:
                    del el.attrib[a]
    return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode()


def summarize(data: bytes) -> dict:
    root = parse(data)
    notes = root.xpath("//*[local-name()='note']")
    pitched = [n for n in notes if n.xpath("./*[local-name()='pitch']")]
    rests = [n for n in notes if n.xpath("./*[local-name()='rest']")]
    parts = root.xpath("/*/*[local-name()='part']")
    measures = max((len(p.xpath("./*[local-name()='measure']")) for p in parts), default=0)
    return {"parts": len(parts), "measures": measures, "notes": len(pitched), "rests": len(rests),
            "chords": len(root.xpath("//*[local-name()='note']/*[local-name()='chord']"))}

# ---------------------------------------------------------------- HTTP API

@app.post("/api/jobs")
async def create_job(file: UploadFile | None = File(None), url: str = Form("")):
    if file is not None and file.filename:
        data = await file.read(MAX_BYTES + 1)
        name = file.filename
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "file too large")
    elif url.strip():
        data, name = await _in_thread(fetch_url, url.strip())
    else:
        raise HTTPException(400, "upload a file or give a URL")
    kind = sniff(data)
    if not kind:
        raise HTTPException(415, "that isn't a PDF or an image (PNG, JPEG, TIFF)")
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    d.mkdir()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)[:60] or "score"
    src = d / f"{stem}.{kind}"
    src.write_bytes(data)
    job = {
        "id": jid, "created": time.time(), "status": "queued", "name": name, "source_url": url.strip() or None,
        "input": src.name, "kind": kind, "pages": [], "max_pages": MAX_PAGES,
        "engines": {e: {"status": "queued", "files": []} for e in ENGINES},
    }
    save(jid, job)
    with lock:
        pending.append(jid)
    work_q.put(jid)
    return {"id": jid}


async def _in_thread(fn, *a):
    import asyncio  # noqa: PLC0415
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job_dir(jid)
    job = load(jid)
    with lock:
        job["queue_position"] = pending.index(jid) if jid in pending else None
    job["engine_info"] = ENGINE_INFO
    return job


@app.get("/api/jobs/{jid}/input/{path:path}")
def get_input(jid: str, path: str):
    d = job_dir(jid)
    p = (d / path).resolve()
    if not p.is_relative_to(d.resolve()) or not p.is_file() or "out" in p.relative_to(d.resolve()).parts[:1]:
        raise HTTPException(404)
    return FileResponse(p)


def engine_file(jid: str, eng: str, f: str) -> Path:
    d = job_dir(jid)
    if eng not in ALL_ENGINES:
        raise HTTPException(404)
    base = (d / "out" / eng).resolve()
    p = (base / f).resolve()
    if not p.is_relative_to(base) or not p.is_file():
        raise HTTPException(404)
    return p


@app.get("/api/jobs/{jid}/out/{eng}/log")
def get_log(jid: str, eng: str):
    d = job_dir(jid)
    if eng not in ALL_ENGINES:
        raise HTTPException(404)
    p = d / "out" / f"{eng}.log"
    text = p.read_text(errors="replace") if p.exists() else ""
    # collapse carriage-return progress bars to their final state
    text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
    return PlainTextResponse(text[-20000:])


@app.get("/api/jobs/{jid}/out/{eng}/file/{f:path}")
def download(jid: str, eng: str, f: str):
    p = engine_file(jid, eng, f)
    name = f"{eng}-{p.name}"
    return FileResponse(p, filename=name)


@app.get("/api/jobs/{jid}/out/{eng}/xml/{f:path}")
def xml(jid: str, eng: str, f: str, mode: str = "pretty"):
    if mode not in ("raw", "pretty", "semantic"):
        raise HTTPException(400, "mode must be raw, pretty or semantic")
    p = engine_file(jid, eng, f)
    try:
        return PlainTextResponse(normalize(read_musicxml(p), mode), media_type="application/xml")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"couldn't parse {f}: {e}")


@app.get("/api/health")
def health():
    return {"ok": True, "queue": len(pending)}


@app.get("/")
@app.get("/j/{jid}")
def index(jid: str | None = None):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_err(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def _startup() -> None:
    # Re-queue anything interrupted by a restart.
    for d in sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime):
        try:
            j = load(d.name)
        except Exception:  # noqa: BLE001
            continue
        if j.get("status") in ("queued", "running"):
            for name in list(j["engines"]):
                e = j["engines"][name]
                if e["status"] in ("queued", "running") and name not in ENGINES:
                    del j["engines"][name]
                elif e["status"] == "running":
                    e["status"] = "queued"
            save(d.name, j)
            pending.append(d.name)
            work_q.put(d.name)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()


_startup()
