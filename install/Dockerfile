# LiteLLM Proxy with curl for ECS container health checks.
# Upstream image is Wolfi (apk), runs as root, and does not ship curl.
FROM ghcr.io/berriai/litellm:main-stable

USER root
RUN apk add --no-cache curl
