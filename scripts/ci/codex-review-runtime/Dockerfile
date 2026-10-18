# syntax=docker/dockerfile:1
FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315
RUN --mount=type=secret,id=proxy_ca,required=true apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends python3 git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=proxy_ca,required=true NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca npm install --prefix /opt/code --no-save --ignore-scripts --registry https://registry.npmjs.org/ --userconfig /dev/null @openai/codex@0.151.0 && NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca npm install --prefix /opt/security --no-save --ignore-scripts --registry https://registry.npmjs.org/ --userconfig /dev/null @openai/codex-security@0.2.0
WORKDIR /state
COPY --chmod=0555 container.py /opt/reviewer/container.py
ENV HOME=/state CODEX_HOME=/state/home
ENTRYPOINT ["python3", "-I", "/opt/reviewer/container.py"]
