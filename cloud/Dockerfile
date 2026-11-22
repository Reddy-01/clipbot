FROM python:3.12-slim-bookworm AS llama-builder
ARG LLAMA_REVISION=8a97fa98ab1a454709bbf5ac979661feaeadec40
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates cmake curl \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /src && curl -fsSL --retry 3 "https://codeload.github.com/ggml-org/llama.cpp/tar.gz/${LLAMA_REVISION}" \
    | tar -xz --strip-components=1 -C /src
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
      -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DLLAMA_BUILD_SERVER=OFF -DLLAMA_CURL=OFF \
    && cmake --build /build --target llama-completion -j 2

FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
ENV CLIPBOT_HOME=/data CLIPBOT_TOOLS=/opt/clipbot-tools PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg fonts-liberation libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY cloud/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt
COPY cloud/fetch-models.sh cloud/models.sha256 /tmp/clipbot/
RUN sh /tmp/clipbot/fetch-models.sh && rm -rf /tmp/clipbot
COPY --from=llama-builder /build/bin/llama-completion /opt/clipbot-tools/llama/llama-completion
WORKDIR /app
COPY clipbot.py creator_studio.py face_emoji.py monitor.py pipeline_addons.py postiz_drafts.py shorts_captions.py subject_tracking.py youtube_api.py ui.html ./
RUN groupadd --gid 10001 clipbot && useradd --uid 10001 --gid 10001 --no-create-home clipbot \
    && install -d -o 10001 -g 10001 -m 0750 /data /data/input /data/output /data/work
USER 10001:10001
EXPOSE 8765
CMD ["python", "clipbot.py", "serve", "--bind", "0.0.0.0"]
