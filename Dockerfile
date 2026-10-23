FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# Optional corporate/TLS-intercepting proxy CA: docker build --secret id=ca,src=/path/ca.crt
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY chiller/ chiller/
COPY plc/ plc/
COPY tools/ tools/
COPY hmi_web/ hmi_web/
ENV PYTHONUNBUFFERED=1
