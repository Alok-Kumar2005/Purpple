FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /dashboard

# Only the packages the dashboard needs
RUN uv pip install --system \
    streamlit>=1.35.0 \
    requests>=2.32.0 \
    pandas>=2.2.0

COPY dashboard/app.py ./app.py

EXPOSE 8501

CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]