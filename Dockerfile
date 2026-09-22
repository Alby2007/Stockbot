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

# Drop root: the services only need to read the installed package and reach
# Postgres over the network.
RUN useradd --system --uid 10001 stockbot
USER stockbot

ENV PYTHONUNBUFFERED=1
# The package is pip-installed to site-packages, so migrate.py's
# repo-relative default doesn't exist here -- point it at the copy.
ENV MIGRATIONS_DIR=/app/migrations
