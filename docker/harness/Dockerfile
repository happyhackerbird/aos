# Pinned OpenShell harness image. Build: docker build -t aos-harness:mvp docker/harness
# Bump a version or checksum by changing the ARG below, then rebuild and re-check.
ARG BASE_IMAGE=node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG TARGETARCH
ARG CLAUDE_VERSION=2.1.296
ARG CODEX_VERSION=0.162.1
ARG PI_VERSION=1.1.0
ARG OMP_VERSION=18.8.3
ARG OMP_SHA256_ARM64=170852b7815ef74e1212f87e69fa7f2730f31716479177df436d3eaa832d8ca5
ARG OMP_SHA256_AMD64=8cb6d6a0035a3c5d9c40a58852ca361569eafc2cecbbeef4521e0fefc8ccbc37
ARG UV_VERSION=0.13.0
ARG PYTHON_SERIES=3.12
ARG PLAYWRIGHT_VERSION=1.63.0
ARG POETRY_VERSION=2.1.3

ENV UV_PYTHON_INSTALL_DIR=/usr/local/share/uv-python \
    UV_PYTHON_PREFERENCE=only-managed \
    PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/ms-playwright

USER root

# System tools and the app-verification stack, pinned by the base image digest;
# the resolved versions are recorded in /etc/aos-harness/versions.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git ripgrep fd-find procps tmux lsof python3 python3-venv python3-pip jq openssh-client \
      postgresql chromium fonts-liberation \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd && rm -rf /var/lib/apt/lists/*

# Claude Code's postinstall must run (no --ignore-scripts for it).
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm install -g --ignore-scripts "@openai/codex@${CODEX_VERSION}" "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force

# omp is a single static binary, not an npm package. TARGETARCH comes from BuildKit;
# the legacy builder leaves it empty, so fall back to dpkg.
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "${arch}" in \
      arm64) asset=omp-linux-arm64; sum="${OMP_SHA256_ARM64}" ;; \
      amd64) asset=omp-linux-x64; sum="${OMP_SHA256_AMD64}" ;; \
      *) echo "unsupported architecture '${arch}'" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/omp "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/${asset}" \
    && echo "${sum}  /usr/local/bin/omp" | sha256sum -c - \
    && chmod 0555 /usr/local/bin/omp

# uv, a uv-managed Python, Playwright's Chromium and Poetry, installed system-wide.
# Poetry is a uv tool on the managed Python (freelance_bot's pyproject uses Poetry's caret
# specifiers, which uv refuses; Poetry installs from that repository's lock file).
RUN python3 -m venv /usr/local/lib/uv \
    && /usr/local/lib/uv/bin/pip install --no-cache-dir "uv==${UV_VERSION}" \
    && ln -s /usr/local/lib/uv/bin/uv /usr/local/bin/uv \
    && uv python install "${PYTHON_SERIES}" \
    && uv tool run --from "playwright==${PLAYWRIGHT_VERSION}" playwright install chromium \
    && UV_TOOL_DIR=/usr/local/share/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
       uv tool install --python "${PYTHON_SERIES}" "poetry==${POETRY_VERSION}"

# Image ENV does not reach processes started inside an OpenShell sandbox, but
# /etc/profile.d does, so the three variables are exported there as well.
RUN printf '%s\n' \
      'export UV_PYTHON_INSTALL_DIR=/usr/local/share/uv-python' \
      'export UV_PYTHON_PREFERENCE=only-managed' \
      'export PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/ms-playwright' \
      > /etc/profile.d/aos-harness.sh \
    && chmod 0644 /etc/profile.d/aos-harness.sh

RUN usermod -l sandbox -d /home/sandbox -m node && groupmod -n sandbox node \
    && mkdir -p /sandbox && chown 1000:1000 /sandbox

RUN mkdir -p /etc/aos-harness \
    && { \
      echo "node=$(node --version)"; \
      echo "npm=$(npm --version)"; \
      echo "claude=$(claude --version 2>&1 | head -n1)"; \
      echo "codex=$(codex --version 2>&1 | head -n1)"; \
      echo "pi=$(pi --version 2>&1 | head -n1)"; \
      echo "omp=$(omp --version 2>&1 | head -n1)"; \
      echo "uv=$(uv --version)"; \
      echo "python=$(uv run --no-project --python "${PYTHON_SERIES}" python --version)"; \
      echo "playwright=$(uv tool run --from "playwright==${PLAYWRIGHT_VERSION}" playwright --version)"; \
      echo "poetry=$(poetry --version)"; \
      echo "chromium=$(chromium --version)"; \
      echo "postgresql=$(psql --version)"; \
      echo "tmux=$(tmux -V)"; \
      echo "debian=$(cat /etc/debian_version)"; \
      echo "base=${BASE_IMAGE}"; \
    } > /etc/aos-harness/versions \
    && chmod -R a+rX /usr/local/share /usr/local/lib/uv /etc/aos-harness \
    && rm -rf /home/sandbox/.cache /tmp/*

# aos-agent: the in-sandbox MCP server and Claude hook entry points (agents that talk, U2),
# one standard-library Python file; its own layers so a change does not rebuild the installs.
# A plain COPY plus chmod rather than `COPY --chmod`, which the legacy builder refuses
# (this Dockerfile builds on it too: see the TARGETARCH fallback above).
COPY aos_agent.py /usr/local/bin/aos-agent
COPY aos_pi.ts /usr/local/lib/aos-agent/aos_pi.ts
RUN chmod 0755 /usr/local/bin/aos-agent \
    && chmod 0644 /usr/local/lib/aos-agent/aos_pi.ts
RUN install -o root -g root -m 0444 /dev/null /usr/local/lib/aos-agent/attach.lock

USER 1000:1000
WORKDIR /sandbox
ENV HOME=/home/sandbox PI_CODING_AGENT_DIR=/tmp/pi-agent CLAUDE_CONFIG_DIR=/tmp/claude-home CODEX_HOME=/tmp/codex-home
