# EUDR Platform runtime image.
# Shapely / pyproj / psycopg[binary] ship manylinux wheels, so the slim image
# needs no extra system libraries.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install dependencies (and the package) from the pyproject metadata. The app
# source is also present on PYTHONPATH so Jinja templates load from the tree.
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
