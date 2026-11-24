# syntax=docker/dockerfile:1
#
# FFmpeg API for Railway: one image for both roles (API and worker).
#
# FFmpeg 9.1 comes from wader/static-ffmpeg (static builds with x264, x265, libvpx,
# SVT-AV1, dav1d, opus, lame, vorbis, webp, ...), copied into a slim Python image.
# Pins checked 2026-11-24 on Docker Hub (index digests).
ARG FFMPEG_IMAGE=mwader/static-ffmpeg:9.1@sha256:dd09509d8c2152bfa601c8c20e3dd166bde906d37951b9f7e8bbd922267f5ac9
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:6d68eb6310a906f26c7638eb6ea7557d4261662df3181af2f80f1f9a7e34c549

FROM ${FFMPEG_IMAGE} AS ffmpeg

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
# FFmpeg jobs run as this unprivileged user (no access to the worker's environment).
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin ffmpegjob \
 && ffmpeg -hide_banner -version | head -n 1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --retries 5 -r requirements.txt
COPY app ./app
EXPOSE 8080
# Role from ROLE (api or worker), set per service in the template.
ENTRYPOINT ["python", "-m", "app"]

