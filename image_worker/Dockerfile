FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@0.145.0 \
    && npm cache clean --force
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /app/venv && /app/venv/bin/pip install --no-cache-dir -r /app/requirements.txt \
    && mkdir -p /var/lib/ember-images /home/node/.codex \
    && chown -R node:node /var/lib/ember-images /home/node/.codex
COPY --chown=node:node . /app/image_worker
ENV PATH="/app/venv/bin:${PATH}" \
    CODEX_HOME=/home/node/.codex \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER node
EXPOSE 8311
CMD ["python", "-m", "image_worker"]
