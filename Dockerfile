FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN pip install uv
WORKDIR /opt/dreamteam
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
RUN git config --global user.name dreamteam \
 && git config --global user.email dreamteam@localhost \
 && git config --global --add safe.directory '*'
WORKDIR /swarm
EXPOSE 8765
ENTRYPOINT ["dreamteam"]
# the container port is fixed (8765) whatever dreamteam.toml says: port mappings point at it;
# /swarm is a DreamTeam home (sessions page + every session) or a single-swarm project (`run`)
CMD ["serve", "/swarm", "--host", "0.0.0.0", "--port", "8765", "--no-console", "--no-browser"]
