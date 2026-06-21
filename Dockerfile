# Servicio de navegación: genera infografías con Gemini web (sesión del dueño).
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
