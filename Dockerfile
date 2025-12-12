# Multi-stage build for optimal image size
FROM python:3.11-slim AS builder

# Set working directory
WORKDIR /app

# Copy pyproject.toml for dependency installation
COPY pyproject.toml ./

# Install dependencies from pyproject.toml
RUN pip install --no-cache-dir --user .

# Final stage
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy installed dependencies from builder stage
COPY --from=builder /root/.local /root/.local

# Copy application code
COPY app ./app

# Create files directory (fallback if no volume is mounted)
RUN mkdir -p /app/files

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Environment variables (can be overridden at runtime)
ENV FILES_DIRECTORY=/app/files
ENV CORS_ORIGINS=*

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "app"]
