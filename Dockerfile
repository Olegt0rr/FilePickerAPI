# Multi-stage build for optimal image size
FROM python:3.11-slim AS builder

# Set working directory
WORKDIR /app

# Install dependencies directly
RUN pip install --no-cache-dir --user fastapi==0.104.1 uvicorn[standard]==0.24.0 pydantic==2.5.0

# Final stage
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy installed dependencies from builder stage
COPY --from=builder /root/.local /root/.local

# Copy application code
COPY main.py ./

# Create files directory
RUN mkdir -p /app/files

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Environment variables (can be overridden at runtime)
ENV FILES_DIRECTORY=/app/files
ENV CORS_ORIGINS=*

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "main.py"]
