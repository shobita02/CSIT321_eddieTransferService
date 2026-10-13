FROM python:3.12-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt
COPY katgateway ./katgateway
RUN useradd --create-home gateway
USER gateway
EXPOSE 8000
CMD ["uvicorn", "katgateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
