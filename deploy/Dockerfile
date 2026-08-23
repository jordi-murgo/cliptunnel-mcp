FROM python:3.13-slim

LABEL org.opencontainers.image.title="cliptunnel-mcp repeater"
LABEL org.opencontainers.image.description="Zero-knowledge HTTPS relay for cliptunnel-mcp"
LABEL org.opencontainers.image.source="https://github.com/jordi-murgo/cliptunnel-mcp"

# Install the core package (cryptography included as a required dep).
# No [server] extra needed — the repeater is stdlib-only.
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
WORKDIR /app
RUN pip install --no-cache-dir -e .

# Repeater env vars (override at runtime).
ENV REPEATER_HOST=0.0.0.0
ENV REPEATER_PORT=8443
ENV REPEATER_TOKENS=""

EXPOSE 8443

# Run the repeater.
CMD ["python", "-m", "cliptunnel_mcp.repeater"]