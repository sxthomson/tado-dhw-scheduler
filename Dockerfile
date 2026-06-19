FROM python:3.11-alpine

WORKDIR /app

# Install Timezone Data and temporary Build Dependencies
# We add build-base (C compiler) and yaml-dev to compile PyYAML on ARM64
RUN apk add --no-cache tzdata \
    && apk add --no-cache --virtual .build-deps gcc musl-dev yaml-dev build-base

COPY requirements.txt .

# Install dependencies, then immediately delete the C compiler to save space
RUN pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

# Copy Source Code
COPY src/ /app/src/

# Copy the Config File to a safe fallback location
COPY config/config.yaml /app/default_config.yaml

# Ensure the mount directories exist
RUN mkdir -p /app/storage /app/config

# Set Python Path
ENV PYTHONPATH=/app/src

CMD ["python", "src/main.py"]