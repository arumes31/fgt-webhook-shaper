# Python 3.13 slim, pinned by digest (update the digest to pick up base image fixes)
FROM python:3.13-slim@sha256:27b7c9d4f13ca20bb1170148f02095d50a5dfce1f7f2dabf5ca273727e43b7ea

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
