# OMR Compare

Proof-of-concept that runs a score (PDF or image, upload or URL) through three
open-source OMR engines and shows the results side by side:

| engine    | version | notes |
|-----------|---------|-------|
| Audiveris | 5.11.0  | .deb from GitHub releases; needs libgtk-3 even in `-batch` |
| oemer     | 0.1.8   | **disabled by default** (`ENGINES` env; add `oemer` to enable). onnxruntime pinned to 1.17.3 (newer rejects its ConvTranspose pads); batch size patched 16→4 (`OEMER_BATCH`) to fit in memory; very slow on CPU |
| homr      | 0.7.1   | `homr[cpu]` |

The browser renders each MusicXML with OpenSheetMusicDisplay and diffs any two outputs
(jsdiff + diff2html), in raw, reformatted, or "music only" mode (layout/metadata stripped).

## Run

    docker compose up -d --build

- One job at a time and one engine at a time (container capped at 4 GB / 3 CPUs).
- Jobs live in `./data/jobs/<id>/` and are deleted after `KEEP_HOURS` (72).
- oemer/homr model weights download on first use. Warming them up during `docker build` OOM'd the host.
- URL fetches refuse non-public addresses (SSRF guard), and so do their redirects.

## Going live (omr.mhciael.com)

1. Delete the `ports:` block in `compose.yaml` and run `docker compose up -d`.
2. NPM proxy host `omr.mhciael.com` → `http://omr:8000`. Set `client_max_body_size 50m;` and
   `proxy_read_timeout 120s;` in Advanced.
