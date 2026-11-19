# OMR Compare (proof of concept)

Runs a score (PDF or image, uploaded or fetched from a URL) through open-source optical music
recognition engines and shows the results side by side:

- the original page(s) next to an [OpenSheetMusicDisplay](https://opensheetmusicdisplay.org/)
  rendering of each engine's MusicXML, with quick stats (parts / measures / notes / rests),
  downloads and engine logs;
- a MusicXML diff between any two outputs (jsdiff + diff2html): "music only" (layout,
  fonts and metadata stripped), full reformatted XML, or raw.

| engine | version | notes |
|---|---|---|
| [Audiveris](https://github.com/Audiveris/audiveris) | 5.11.0 | .deb from GitHub releases. Needs libgtk-3 even in `-batch`, and its desktop-integration postinst is no-op'd. |
| [homr](https://github.com/liebharc/homr) | 0.7.1 | `homr[cpu]` (onnxruntime). |
| [oemer](https://github.com/BreezeWhite/oemer) | 0.1.8 | **Off by default.** onnxruntime is pinned to 1.17.3 because newer versions reject its ConvTranspose pads. Batch size is patched 16→4 (`OEMER_BATCH`) to fit in memory. Very slow on CPU. |

## Quick start

```sh
git clone git@github.com:hcientist/omr-poc.git && cd omr-poc
docker compose up -d --build      # first build ≈ 3 GB image, a few minutes
open http://localhost:8487        # or xdg-open
```

The first job is slower: homr (and oemer, if enabled) download their model weights on first use.
Warming them up during `docker build` was skipped on purpose, because it OOM'd a small build host.

Knobs (shell env or a `.env` file next to `compose.yaml`):

| var | default | |
|---|---|---|
| `OMR_ENGINES` | `audiveris,homr` | add `oemer` to try it, e.g. `audiveris,oemer,homr` |
| `OMR_MEM` | `6g` | container memory cap. The engines run one at a time. |
| `OMR_THREADS` | `4` | `OMP_NUM_THREADS` for the ONNX engines |
| `OMR_BIND` / `OMR_PORT` | `127.0.0.1` / `8487` | where the web UI listens |
| `OMR_CORS_ORIGINS` | `*` | origins allowed to fetch outputs cross-origin (comma list; empty disables CORS) |

Other settings live in `compose.yaml`: `MAX_UPLOAD_MB` (40) and `MAX_PAGES` (6; PDF pages given to
homr/oemer, while Audiveris gets the whole PDF). There's also `ENGINE_TIMEOUT` (900 s), `OEMER_TIMEOUT`
(3000 s per page) and `KEEP_HOURS` (72; old jobs are deleted).

**Apple Silicon:** the image is x86_64 only, because Audiveris publishes no arm64 Linux .deb. It runs
under Docker Desktop's Rosetta/QEMU emulation, but expect it to be several times slower. Build with
`DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose up -d --build`. The native alternatives are
Audiveris's macOS arm64 .dmg and `uvx --from 'homr[cpu]' homr page.png`.

## Hardware

Everything here runs on CPU. Rough guide:

- **Minimum** (Audiveris + homr): 4 modern cores, 8 GB RAM with ~4 GB free for the container.
  Expect ~15–30 s per page per engine.
- **Comfortable**: 8+ cores (recent Ryzen / Core i7 / Apple M-series under emulation), 16 GB RAM.
- **oemer**: wants 6–8 GB headroom at its default batch size. It's the slowest of the three by far:
  tens of minutes per page on a 2-core NUC with swapping.
- **GPU**: optional. homr (`homr[cuda]`, NVIDIA + CUDA 12, or ROCm builds) and oemer
  (`onnxruntime-gpu`) can use one, and that's where a GPU would pay off. This image doesn't wire
  that up; it would need an NVIDIA base image plus `--gpus all`. Audiveris is CPU-only Java either way.

## API

- `POST /api/jobs`: multipart `file=` or form `url=` → `{"id": ...}`
- `GET /api/jobs/{id}`: status, per-engine files and stats
- `GET /api/jobs/{id}/out/{engine}/file/{name}`: raw output (.mxl / .musicxml)
- `GET /api/jobs/{id}/out/{engine}/xml/{name}?mode=raw|pretty|semantic`: uncompressed / normalized XML
- `GET /api/jobs/{id}/out/{engine}/log`

URL fetches refuse non-public addresses, including after redirects.

## Behind a reverse proxy

Set `OMR_BIND` to an address your proxy can reach, or join the proxy's docker network in a
`compose.override.yaml` and proxy to `omr:8000`. Allow bodies of ~50 MB (`client_max_body_size 50m;`).
