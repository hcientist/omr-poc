FROM ghcr.io/astral-sh/uv:0.9.5 AS uv

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 python3-dev poppler-utils \
        libgl1 libglib2.0-0 libgtk-3-0t64 fontconfig fonts-dejavu-core libfreetype6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

# --- Audiveris (bundles its own JRE) ---
ARG AUDIVERIS_VERSION=5.11.0
RUN curl -fsSL -o /tmp/audiveris.deb \
      https://github.com/Audiveris/audiveris/releases/download/${AUDIVERIS_VERSION}/Audiveris-${AUDIVERIS_VERSION}-ubuntu24.04-x86_64.deb \
    && apt-get update \
    && { apt-get install -y --no-install-recommends /tmp/audiveris.deb \
         || { printf '#!/bin/sh\nexit 0\n' > /var/lib/dpkg/info/audiveris.postinst && dpkg --configure audiveris; }; } \
    && test -x /opt/audiveris/bin/Audiveris \
    && rm -rf /tmp/audiveris.deb /var/lib/apt/lists/*

# --- homr (own venv) ---
RUN uv venv /opt/homr --python python3.12 \
    && uv pip install -p /opt/homr 'homr[cpu]==0.7.1'

# --- oemer (own venv; skip onnxruntime-gpu, pin numpy<2 for the 2023-era code) ---
RUN uv venv /opt/oemer --python python3.12 \
    && uv pip install -p /opt/oemer --no-deps oemer==0.1.8 \
    && uv pip install -p /opt/oemer 'onnxruntime==1.17.3' 'opencv-python-headless>=4.5.3.56' matplotlib pillow \
         scipy 'scikit-learn>=1.2' typing-extensions 'numpy<2' \
    && sed -i 's/batch_size: int = 16/batch_size: int = int(__import__("os").environ.get("OEMER_BATCH", "4"))/' \
         /opt/oemer/lib/python3.12/site-packages/oemer/inference.py \
    && grep -q OEMER_BATCH /opt/oemer/lib/python3.12/site-packages/oemer/inference.py

# --- web app ---
RUN uv venv /opt/app --python python3.12 \
    && uv pip install -p /opt/app fastapi 'uvicorn[standard]' python-multipart lxml

# Model weights for oemer/homr download on first use (warming up here OOMs the build host).

COPY app /app
WORKDIR /app
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["/opt/app/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
