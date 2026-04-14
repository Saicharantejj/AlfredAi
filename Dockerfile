FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALFRED_AUTOSTART_WHATSAPP_BRIDGE=false

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# We use 8080 as it is the standard for Railway
EXPOSE 8080

# Explicitly binding to 8080 to match Railway's internal routing
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
