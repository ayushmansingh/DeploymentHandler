"""Generate the per-app container recipe.

Every app becomes exactly one container laid out the same way:

    nginx  :80   <- the only published port
      |-- /       static frontend build (if the app has one)
      '-- /api    proxied to 127.0.0.1:8000 (the Python backend, if any)

Serving both halves from one origin is what makes the frontend's relative
"/api/..." calls work without any per-app configuration, and publishing a
single port per app keeps the registry simple.
"""
from __future__ import annotations

from pathlib import Path

from .detect import Spec

DEFAULT_PYTHON = "3.11"
DEFAULT_NODE = "20"

NGINX_CONF = """\
server {{
    listen 80;
    server_name _;
    client_max_body_size 50m;

{root_block}
{api_block}
}}
"""

STATIC_ROOT_BLOCK = """\
    root /var/www/html;
    index index.html;

    location / {
        # Single-page apps own their routing: fall back to index.html so a
        # deep link like /reports does not 404 on refresh.
        try_files $uri $uri/ /index.html;
    }
"""

PROXY_ALL_BLOCK = """\
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
"""

API_BLOCK = """\
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
"""

START_SH = """\
#!/bin/bash
# Start the backend (if this app has one) and nginx, and make the container
# exit as soon as either dies so Docker's restart policy can take over.
set -o pipefail

term() {{ kill 0 2>/dev/null; }}
trap term SIGTERM SIGINT

{backend_launch}
nginx -g 'daemon off;' &
NGINX_PID=$!

wait -n
EXIT=$?
term
exit $EXIT
"""

BACKEND_LAUNCH = """\
cd /app/{backend_path}
{start_command} 2>&1 | sed -u 's/^/[backend] /' &
BACKEND_PID=$!
cd /app
"""


def _dockerfile(spec: Spec, python_version: str, node_version: str) -> str:
    lines: list[str] = ["# syntax=docker/dockerfile:1", ""]

    if spec.frontend:
        fe = spec.frontend
        lines += [
            f"FROM node:{node_version}-alpine AS frontend",
            "WORKDIR /fe",
            # Copy manifests first so dependency installation caches across
            # rebuilds when only application code changed.
            f"COPY {fe.path}/package.json ./",
            f"COPY {fe.path}/package-lock.json* {fe.path}/npm-shrinkwrap.json* ./",
            "RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund",
            f"COPY {fe.path}/ ./",
            f"RUN {fe.build}",
            "",
        ]

    lines += [
        f"FROM python:{python_version}-slim",
        "ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1",
        "RUN apt-get update"
        " && apt-get install -y --no-install-recommends nginx curl"
        " && rm -rf /var/lib/apt/lists/*",
        "WORKDIR /app",
    ]

    if spec.backend:
        be = spec.backend
        if be.requirements:
            lines += [
                f"COPY {be.requirements} /tmp/requirements.txt",
                "RUN pip install -r /tmp/requirements.txt",
            ]
        # Safety net: generated projects routinely forget to list the server
        # they are started with. Installing these is cheap and removes the
        # single most common first-deploy failure.
        lines.append("RUN pip install uvicorn gunicorn")

    lines += [
        "COPY . /app",
        "COPY .launcher/nginx.conf /etc/nginx/sites-available/default",
        "COPY .launcher/start.sh /start.sh",
        "RUN chmod +x /start.sh",
    ]

    if spec.frontend:
        lines += [
            "RUN rm -rf /var/www/html && mkdir -p /var/www/html",
            f"COPY --from=frontend /fe/{spec.frontend.output}/ /var/www/html/",
        ]

    lines += [
        "EXPOSE 80",
        'HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\',
        '  CMD curl -fsS http://127.0.0.1:80/ >/dev/null || exit 1',
        'CMD ["/start.sh"]',
        "",
    ]
    return "\n".join(lines)


def _nginx_conf(spec: Spec) -> str:
    if spec.frontend and spec.backend:
        return NGINX_CONF.format(root_block=STATIC_ROOT_BLOCK, api_block=API_BLOCK)
    if spec.frontend:
        return NGINX_CONF.format(root_block=STATIC_ROOT_BLOCK, api_block="")
    return NGINX_CONF.format(root_block="", api_block=PROXY_ALL_BLOCK)


def _start_sh(spec: Spec) -> str:
    if spec.backend:
        launch = BACKEND_LAUNCH.format(
            backend_path=spec.backend.path,
            start_command=spec.backend.start,
        )
    else:
        launch = ""
    return START_SH.format(backend_launch=launch)


def _dockerignore() -> str:
    return "\n".join(
        ["node_modules", ".venv", "venv", "__pycache__", "*.pyc", ".git", ".pytest_cache", ""]
    )


def write_build_context(
    root: Path,
    spec: Spec,
    python_version: str = DEFAULT_PYTHON,
    node_version: str = DEFAULT_NODE,
) -> None:
    """Drop the generated build files into the extracted project directory."""
    hidden = root / ".launcher"
    hidden.mkdir(exist_ok=True)
    (hidden / "nginx.conf").write_text(_nginx_conf(spec))
    (hidden / "start.sh").write_text(_start_sh(spec))
    (root / "Dockerfile").write_text(_dockerfile(spec, python_version, node_version))
    (root / ".dockerignore").write_text(_dockerignore())
