ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY mcp_server/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY mcp_server/ ./mcp_server/

CMD ["python3", "-m", "mcp_server.main"]
