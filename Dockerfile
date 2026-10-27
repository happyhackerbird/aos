FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent_os ./agent_os
RUN pip install --no-cache-dir . && useradd --create-home app && mkdir /data && chown app:app /data
ENV BUS_DATA_DIR=/data
USER app
CMD ["python", "-m", "agent_os", "serve"]
