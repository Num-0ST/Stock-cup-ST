FROM python:3.13-slim
WORKDIR /srv
# fonts for PDF export (Latin + Thai + Lao) + free OCR for bill scanning (Thai / Lao / English)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-tlwg-loma-ttf fonts-lao \
       tesseract-ocr tesseract-ocr-tha tesseract-ocr-lao tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY migrations ./migrations
COPY app ./app
COPY static ./static
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# run the API as an unprivileged user (entrypoint drops root after fixing the uploads volume ownership)
RUN useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin app \
    && chmod 0755 /usr/local/bin/entrypoint.sh && mkdir -p /data/uploads && chown app:app /data/uploads
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
