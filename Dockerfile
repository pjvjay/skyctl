# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# syntax=docker/dockerfile:1.7
# One image for every entry point and every cloud:
#   skyctl serve                  HTTP service: /mcp, /runs, /invoke, /tick  (ECS, Container Apps, Cloud Run, k8s)
#   skyctl invoke '<json>'        one Invocation: what every scheduled job runs (RunTask, ACA Job, Cloud Run Job, CronJob)
#   skyctl worker [--once]        queue consumer (ECS worker, ACA event job, KEDA ScaledJob)
#   skyctl daemon                 schedules + worker in one process (Compose / VM)
#   Lambda                        same image; image_config entry_point ["python","-m","awslambdaric"],
#                                 command ["skyctl.platform.lambda_handler.handler"]  (needs the [aws] extra)
#
# Build:  docker build -t skyctl:local .
# Run:    docker run --rm -e SKYCTL_SECRETS=env --env-file .env skyctl:local accounts

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
# LICENSE and NOTICE ship inside the installed package metadata (Apache-2.0 §4(d)).
COPY pyproject.toml README.md LICENSE NOTICE AUTHORS ./
COPY skyctl ./skyctl
# [all] = agents + mcp/server + aws + azure + gcp adapters. Slim it with
# --build-arg EXTRAS=agents,mcp,aws for a single-cloud image.
ARG EXTRAS=all
RUN python -m venv /opt/venv && /opt/venv/bin/pip install ".[${EXTRAS}]"

FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="skyctl" \
      org.opencontainers.image.description="Bluesky accounts, agents and MCP tools for AWS, Azure, Google Cloud and Kubernetes" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    SKYCTL_STATE_URL=file:///data
RUN useradd --system --uid 10001 --create-home --home-dir /home/skyctl skyctl \
 && mkdir -p /data && chown skyctl:skyctl /data
COPY --from=build /opt/venv /opt/venv
USER skyctl
WORKDIR /home/skyctl
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz')" || exit 1
ENTRYPOINT ["skyctl"]
CMD ["serve"]
