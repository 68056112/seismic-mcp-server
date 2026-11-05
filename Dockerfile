FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp \
    SEISMIC_TZ=Asia/Bangkok

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY seismic_core.py seismic_mcp_server.py ./

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); sys.exit(0)" || exit 1

CMD ["python", "seismic_mcp_server.py"]
