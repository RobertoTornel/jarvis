# Claire in a container - the TEXT/TOOL HALF ONLY.
#
# WHAT THIS IMAGE IS HONESTLY FOR
# ------------------------------
# Claire is a Linux voice assistant bound to one human's login session. A
# container has no session:
#
#   * NO AUDIO. There is no PipeWire/PulseAudio server, no XDG_RUNTIME_DIR and
#     no /dev/snd device unless you explicitly pass `--device /dev/snd` *and*
#     run a sound server inside the container. `sounddevice` may import, but
#     `query_devices(kind="input")` finds nothing usable. Voice input, wake word
#     and TTS playback do not work here. Do not ship this image and tell someone
#     Claire will listen to them.
#   * NO DISPLAY. There is no X11/Wayland server, so the screenshot and GUI
#     automation senses cannot run. Chromium can run headless (that is what the
#     optional browser layer below is for); anything that needs to see the
#     user's screen cannot.
#
# What DOES work: the LLM/tool half - chat, memory/vault, scheduling, web fetch,
# browser (headless), OCR over an image you hand it. That is a useful service
# (an API/toolbox) and a useless voice assistant, and this comment says which
# one you are building.
#
# Run the daemon on the host under `deploy/claire.service` for voice.
#
# SECRETS
# -------
# No key is baked in, and none should ever be: keys arrive at RUN time via
# `--env-file`, a secrets mount, or the environment. There is deliberately no
# `COPY *.env`, no `ARG *_API_KEY` and no `ENV ..._KEY=...` line below.
#
# .DOCKERIGNORE
# -------------
# The build context is the repository, so put this in `.dockerignore` before
# building (the file is not committed by this task, add it yourself):
#
#   .git
#   .venv
#   __pycache__/
#   *.pyc
#   .env
#   *.env
#   config.json
#   .pytest_cache/
#   tests/
#   data/
#   *.nemo
#   *.onnx
#   models/
#
# The `COPY` lines below are deliberately file-by-file for the same reason: the
# context may contain a key file even if the image must not.

# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

# System packages actually needed by the text/tool half:
#   tesseract-ocr   - the binary pytesseract shells out to (screen/OCR sense)
#   libportaudio2   - so `import sounddevice` succeeds and the self-check can
#                     report the TRUTH ("no input device") instead of a missing
#                     shared library. It does not make audio work here.
#   ca-certificates - HTTPS for the LLM endpoint and web fetch
#   curl            - used by the HEALTHCHECK below
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        libportaudio2 \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The src layout needs to be importable for `python -m claire`.
    PYTHONPATH=/app/src \
    # Where a mounted key file is expected. Mount it read-only at run time
    # (`docker run -v /host/claire.env:/home/claire/.config/claire/claire.env:ro`),
    # never COPY one in.
    CLAIRE_ENV_FILE=/home/claire/.config/claire/claire.env

WORKDIR /app

# The text/tool subset on purpose. The full requirements.txt pulls the voice
# stack (faster-whisper, piper, chatterbox, sherpa-onnx, ...) - hundreds of MB
# that cannot function without a session. Install it anyway with:
#   RUN pip install -r requirements.txt
COPY requirements.txt /app/requirements.txt
RUN pip install \
        python-dotenv \
        requests \
        flask \
        beautifulsoup4 \
        lxml \
        html2text \
        Pillow \
        pytesseract \
        playwright \
        keyring

# Playwright's chromium is ~400MB with its libraries, so it is opt-in:
#   docker build --build-arg INSTALL_BROWSER=1 ...
ARG INSTALL_BROWSER=0
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        python -m playwright install --with-deps chromium; \
    fi

COPY src/ /app/src/

# Non-root, and no shell: the image runs as `claire`, not as root, so a
# compromise in a tool handler does not own the container.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin claire \
    && mkdir -p /home/claire/.config/claire /home/claire/.local/share/claire \
    && chown -R claire:claire /home/claire
USER claire

# The health check reports only what CAN be healthy here (vault + key store).
# The voice checks (stt_model, audio_device) are expected to fail inside a
# container, so they are excluded rather than lying; run the full report with
#   docker run --rm <image> python -m claire.platform.service --self-check
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -m claire.platform.service --self-check --json --only vault --only key_store || exit 1

STOPSIGNAL SIGINT
ENTRYPOINT ["python", "-m", "claire"]
