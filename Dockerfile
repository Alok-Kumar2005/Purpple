FROM python:3.12-slim

# Install uv inside the container for ultra-fast package setups
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# Copy configuration files to resolve dependencies
COPY pyproject.toml uv.lock ./

# Install project dependencies globally inside the container
RUN uv pip install --system -r pyproject.toml

# Copy application code into the container workspace
COPY main.py ./
COPY app/ ./app/
COPY pipeline/ ./pipeline/
COPY store_layout.json ./

# Open up the API port
EXPOSE 8000

# Run FastAPI in production-optimized mode
CMD ["fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]