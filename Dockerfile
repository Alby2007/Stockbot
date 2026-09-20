FROM python:3.13-slim

WORKDIR /app

# Build deps for psycopg[binary]/numpy/scipy wheels are not needed since we
# use binary wheels, but libpq is still required at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
