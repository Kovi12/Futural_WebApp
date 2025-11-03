FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    openssh-client \
    build-essential \
    curl \
    procps \
    socat \
    iputils-ping \
    lsof \
    net-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full application source
COPY . .

# Add precompiled autossh binary
COPY autossh/bin/autossh /usr/local/bin/autossh
RUN chmod +x /usr/local/bin/autossh

# Add SSH key directory
RUN mkdir -p /root/.ssh
COPY ssh_keys /root/.ssh
RUN chmod 700 /root/.ssh && \
    chmod 600 /root/.ssh/id_ed25519 && \
    chmod 600 /root/.ssh/config && \
    chmod 644 /root/.ssh/known_hosts

# Ensure both entry scripts are executable
COPY start_backend.sh /start_backend.sh
COPY start_backend_dual.sh /start_backend_dual.sh
RUN chmod +x /start_backend.sh /start_backend_dual.sh

# Default command (legacy backend)
CMD ["/start_backend.sh"]
