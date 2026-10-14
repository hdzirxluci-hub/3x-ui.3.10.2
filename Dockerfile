# 3x-ui panel packaged for Railway.
#
# Railway gives a service exactly ONE public port and terminates TLS at its own
# edge, so the panel (2053), the subscription server (2096) and the Xray
# inbound (10085) all bind to loopback and are split by URL path in nginx.
#
# Mount a Railway Volume at /etc/x-ui or every redeploy wipes your users.

FROM ubuntu:24.04

# Pinned on purpose: the API paths and the inbound payload shape in
# entrypoint.sh were verified against this exact release.
ARG XUI_VERSION=v3.9.0
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq nginx python3 sqlite3 tzdata iproute2 procps \
    && ln -sf /usr/share/zoneinfo/Asia/Tehran /etc/localtime \
    && echo "Asia/Tehran" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Fetch the official release tarball rather than the Docker Hub image, which
# is frequently rate-limited and fails Railway builds.
# The release tarball's only top-level entry is "x-ui/", and inside it the panel
# binary is also called "x-ui". So extracting with --strip-components=1 maps
# x-ui/x-ui onto /usr/local/x-ui -- the very path the directory itself occupies.
# Do NOT pre-create that directory and do NOT strip: extract as-is and let tar
# create /usr/local/x-ui/ itself.
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) ARCH=amd64 ;; \
        arm64) ARCH=arm64 ;; \
        arm)   ARCH=armv7a ;; \
        *)     echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/x-ui.tar.gz \
      "https://github.com/MHSanaei/3x-ui/releases/download/${XUI_VERSION}/x-ui-linux-${ARCH}.tar.gz"; \
    tar -xzf /tmp/x-ui.tar.gz -C /usr/local/; \
    rm -f /tmp/x-ui.tar.gz; \
    chmod +x /usr/local/x-ui/x-ui /usr/local/x-ui/bin/xray-linux-${ARCH}; \
    /usr/local/x-ui/x-ui -v

RUN mkdir -p /etc/x-ui /var/log/x-ui /run/nginx

COPY nginx.conf.template /opt/nginx.conf.template
COPY subserver.py /opt/subserver.py
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# Railway injects PORT; 8080 keeps `docker run` working outside Railway.
ENV HTTP_PORT=8080
EXPOSE 8080

ENTRYPOINT ["/opt/entrypoint.sh"]
