FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential iptables \
 && rm -rf /var/lib/apt/lists/*
RUN pip install uv
# sessions without network run their members' commands as this user, whose packets a firewall rejects
# (`dreamteam sandbox` sets it up); git's settings are the system's, so that they hold for both users
RUN useradd --uid 10001 --user-group --create-home --shell /bin/bash dreamteam-agent
WORKDIR /opt/dreamteam
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
RUN git config --system user.name dreamteam \
 && git config --system user.email dreamteam@localhost \
 && git config --system --add safe.directory '*'
WORKDIR /swarm
EXPOSE 8765
ENTRYPOINT ["dreamteam"]
# the container port is fixed (8765) whatever dreamteam.toml says: port mappings point at it;
# /swarm is a DreamTeam home (sessions page + every session) or a single-swarm project (`run`)
CMD ["serve", "/swarm", "--host", "0.0.0.0", "--port", "8765", "--no-console", "--no-browser"]
