ARG BUILD_FROM
FROM $BUILD_FROM

# git: the server commits every doc apply/reject when /config/mcp_docs is a repo.
RUN apk add --no-cache git

WORKDIR /app

COPY mcp_server/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY mcp_server/ ./mcp_server/

CMD ["python3", "-m", "mcp_server.main"]
