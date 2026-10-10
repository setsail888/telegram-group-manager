FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
ARG WITH_OCR=0
RUN if [ "$WITH_OCR" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-chi-sim \
      && rm -rf /var/lib/apt/lists/*; \
    fi
RUN useradd --system --uid 10001 --home /app botuser \
    && mkdir -p /app/data && chown -R botuser:botuser /app
COPY --chown=botuser:botuser app/ /app/app/
USER botuser
CMD ["python", "-m", "app.main"]
