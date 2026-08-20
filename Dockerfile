# Multi-stage / Production Dockerfile for PAC-CLI
# Includes Playwright Chromium, Camoufox (Firefox anti-detect), curl_cffi, and all PAC tools.

FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PAC_RULES_DIR=/home/pwuser/.cache/pac-cli/rules

WORKDIR /app

# Copy project definition and source
COPY . /app

# Install project with full stealth extras, Chromium browser, and Camoufox engine
RUN pip install --no-cache-dir -e ".[all]" && \
    playwright install chromium && \
    camoufox fetch

# Ensure permissions for non-root pwuser (provided by official Playwright base image)
RUN mkdir -p /home/pwuser/.cache/pac-cli && \
    chown -R pwuser:pwuser /app /home/pwuser/.cache

USER pwuser

# Healthcheck to verify PAC diagnostic status
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD pac doctor --compact || exit 1

ENTRYPOINT ["pac"]
CMD ["--help"]
