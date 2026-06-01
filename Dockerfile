FROM python:3.12-slim

# Install uv for ultra-fast package installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# Copy ONLY the API requirements file (not pyproject.toml which pulls in torch/ultralytics)
COPY requirements-api.txt ./

# Install API dependencies globally — no torch, no ultralytics, ~200 MB instead of ~4 GB
RUN uv pip install --system -r requirements-api.txt

# Copy application source
COPY main.py ./
COPY app/ ./app/
COPY pipeline/ ./pipeline/
COPY store_layout.json ./

# Expose the API port
EXPOSE 8000

# Production server
CMD ["fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]