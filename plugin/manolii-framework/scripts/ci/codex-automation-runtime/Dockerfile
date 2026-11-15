# syntax=docker/dockerfile:1
# Credential-free guest for subscription automation. The build context holds only
# the accepted source closure assembled by codex_automation_worker.build_image;
# candidate Git is mounted read-only at run time and never copied here.
FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315
# Optional HTTPS proxy for TLS-intercepting build hosts; DIRECT by default.
ARG APT_PROXY=DIRECT
RUN --mount=type=secret,id=proxy_ca,required=true sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca -o Acquire::https::Proxy="$APT_PROXY" update && apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca -o Acquire::https::Proxy="$APT_PROXY" install -y --no-install-recommends python3 python3-yaml git && rm -rf /var/lib/apt/lists/*
COPY --chmod=0555 opt/automation /opt/automation
WORKDIR /state
ENV HOME=/state PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-I", "/opt/automation/scripts/ci/codex_automation_guest.py"]
