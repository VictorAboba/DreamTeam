# the Debian release pinned (trixie, git 2.47): netoff.trust_workspace's <root>/* needs git 2.46, and the bare tag
# meant bookworm (git 2.39) before trixie, which a machine that pulled it then may still have cached
FROM python:3.13-slim-trixie
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential iptables \
 && rm -rf /var/lib/apt/lists/*
# uv at a fixed version: it installs the image's packages from uv.lock below, and the skills' environments
RUN pip install uv==0.12.23
# sessions without network run their members' commands as this user, whose packets a firewall rejects
# (`dreamteam sandbox` sets it up); git's settings are the system's, so that they hold for both users
RUN useradd --uid 10001 --user-group --create-home --shell /bin/bash dreamteam-agent
WORKDIR /opt/dreamteam
COPY pyproject.toml README.md uv.lock* ./
COPY src ./src
# the versions uv.lock pins, checked against its hashes, go into the system Python as before (`--locked` stops the
# build when uv.lock is behind pyproject.toml); `dreamteam sandbox` of an installed DreamTeam has no uv.lock
# (docker.build_context), and its image gets the version ranges of pyproject.toml
RUN if [ -f uv.lock ]; then \
      uv export --locked --no-dev --no-emit-project --format requirements.txt -o /tmp/requirements.txt \
   && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
   && uv pip install --system --no-cache --no-deps . \
   && rm /tmp/requirements.txt; \
    else pip install .; fi
# no safe.directory: git trusts no repository of another owner here; the processes of a session get their own
# workspace (netoff.trust_workspace), DreamTeam's own git names its repository per command
RUN git config --system user.name dreamteam \
 && git config --system user.email dreamteam@localhost
# the image's own marker: netoff.trust_workspace applies also under a runtime that creates no /.dockerenv
ENV DREAMTEAM_IMAGE=1
WORKDIR /swarm
EXPOSE 8765
ENTRYPOINT ["dreamteam"]
# the container port is fixed (8765) whatever dreamteam.toml says: port mappings point at it;
# /swarm is a DreamTeam home (sessions page + every session) or a single-swarm project (`run`)
CMD ["serve", "/swarm", "--host", "0.0.0.0", "--port", "8765", "--no-console", "--no-browser"]
