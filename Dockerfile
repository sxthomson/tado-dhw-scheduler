FROM python:3.11-alpine

WORKDIR /app

# Install Timezone Data (Crucial for UK DST)
RUN apk add --no-cache tzdata

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Source Code
COPY src/ /app/src/

# Copy the Config File (Baked into image)
COPY config/config.yaml /app/config/config.yaml

# Create directory for persistent tokens
RUN mkdir -p /app/storage

# Set Python Path
ENV PYTHONPATH=/app/src

CMD ["python", "src/main.py"]