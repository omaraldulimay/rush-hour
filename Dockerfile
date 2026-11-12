FROM python:3.15-slim

ARG SERVICE
ENV SERVICE=${SERVICE} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Pull in Debian security fixes published after the base image was built
RUN apt-get update \
    && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# pip, setuptools and wheel are build tools: the base image ships a vulnerable setuptools
# (CVE-2025-47273) and pip vendors another copy, so remove them once the dependencies are installed.
RUN pip install -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

COPY libs ./libs
COPY services ./services
COPY scripts ./scripts

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
# exec so uvicorn is PID 1 and receives SIGTERM
CMD ["sh", "-c", "exec uvicorn services.${SERVICE}.app.main:create_app --factory --host 0.0.0.0 --port 8000"]
