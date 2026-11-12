# Python 3.13 slim, pinned by digest (update the digest to pick up base image fixes)
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

# Install required packages
ARG RUNTIME_APT_REFRESH=manual
RUN printf 'Runtime package refresh: %s\n' "$RUNTIME_APT_REFRESH" \
    && apt-get update && apt-get upgrade -y && apt-get install -y \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel

# Copy application code
COPY app.py .

# Run as an unprivileged user
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin shaper
USER 10001

# Expose port for webhook
EXPOSE 25001

# Run with Gunicorn
CMD ["gunicorn", "--workers", "1", "--threads", "1", "--timeout", "60", "--bind", "0.0.0.0:25001", "app:app"]
