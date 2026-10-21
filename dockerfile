FROM python:3.10-slim

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

# Expose port for webhook
EXPOSE 25001

# Run with Gunicorn
CMD ["gunicorn", "--workers", "1", "--threads", "1", "--timeout", "0", "--bind", "0.0.0.0:25001", "app:app"]
