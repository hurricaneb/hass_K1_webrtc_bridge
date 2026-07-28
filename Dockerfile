ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.11-alpine3.19
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8

# Install build & runtime dependencies
RUN apk add --no-cache \
    ffmpeg \
    ffmpeg-dev \
    libffi \
    libffi-dev \
    openssl \
    openssl-dev \
    jpeg \
    jpeg-dev \
    zlib \
    zlib-dev \
    build-base \
    python3-dev \
    pkgconfig

WORKDIR /app

# Copy requirements & install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Remove build tools to keep image size small
RUN apk del build-base python3-dev pkgconfig ffmpeg-dev libffi-dev openssl-dev jpeg-dev zlib-dev

# Copy application files
COPY creality_webrtc_bridge.py /app/
COPY run.sh /app/

RUN chmod +x /app/run.sh

CMD [ "/app/run.sh" ]
