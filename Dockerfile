# oss-scout v2 运行时（python 完整环境：gh CLI + git + 全部依赖）
# 来源：G:/project/template 模板库约定（非 root + HEALTHCHECK）
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scout_v2 ./scout_v2
COPY dashboard ./dashboard
COPY configs ./configs.seed
COPY scripts ./scripts
COPY cloudflare ./cloudflare
COPY docker ./docker
COPY discover.py health_check.py AGENTS.md ./
COPY tests_v2 ./tests_v2
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Windows 工作区写出的文件权限是 644，必须显式 +x（否则 runc: permission denied）
COPY --chmod=0755 docker/entrypoint-dashboard.sh /entrypoint.sh
RUN useradd -m scout && mkdir -p /data && chown -R scout:scout /app /data
USER scout
ENV OSSCOUT_DASHBOARD_ROOT=/data
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:8787/login || exit 1

EXPOSE 8787
CMD ["python", "-m", "uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8787"]

ENTRYPOINT ["/entrypoint.sh"]
