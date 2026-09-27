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

# matplotlib's font cache defaults to ~/.cache/matplotlib, but `stockbot` is
# a system user with no home -- without MPLCONFIGDIR it lands in a tmpdir
# rebuilt per process, so the first /chart after every restart pays a
# multi-second font scan. Point it at a writable dir and bake the cache
# into the image as the runtime user.
ENV MPLCONFIGDIR=/app/.mplconfig
RUN mkdir -p /app/.mplconfig && chown stockbot /app/.mplconfig
USER stockbot
RUN python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.font_manager; matplotlib.font_manager.fontManager; from matplotlib.figure import Figure; import io; f = Figure(); a = f.add_subplot(); a.set_title('warm'); f.savefig(io.BytesIO(), format='png')"

ENV PYTHONUNBUFFERED=1
# The package is pip-installed to site-packages, so migrate.py's
# repo-relative default doesn't exist here -- point it at the copy.
ENV MIGRATIONS_DIR=/app/migrations
