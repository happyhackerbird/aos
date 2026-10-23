FROM python:3.12-slim
COPY --from=node:24-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends git procps ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/sh node
WORKDIR /workspace
COPY --chown=node:node . /workspace
RUN chown node:node /workspace
USER node
RUN python3 -m venv /home/node/venv \
    && /home/node/venv/bin/pip install --no-cache-dir -e '.[dev]'
ENV PATH="/home/node/venv/bin:${PATH}"
CMD ["python", "-m", "pytest", "-q"]
