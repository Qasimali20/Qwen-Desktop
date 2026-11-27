# Qwen Desktop sandbox: runs model-written Python, JavaScript and shell commands with no network,
# as an unprivileged user.
#
# Python packages are pre-installed on the host into ./site (see build.sh) and copied in: uv's parallel,
# cached downloads are far faster than pip inside `docker build` on this network.
FROM node:22-bookworm-slim AS node
# A few global tools so TypeScript and tests run offline inside the sandbox.
RUN npm install -g --no-fund --no-audit typescript@5.9.3 tsx@4.20.6 && npm cache clean --force

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg PYTHONUNBUFFERED=1 PYTHONPATH=/opt/site \
    PATH=/opt/site/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin NODE_OPTIONS=--max-old-space-size=1536 \
    NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core git jq sqlite3 ripgrep tree file less procps make patch unzip zip xz-utils bc \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN set -e; for t in npm npx; do ln -s ../lib/node_modules/npm/bin/$t-cli.js /usr/local/bin/$t; done; \
    ln -s ../lib/node_modules/typescript/bin/tsc /usr/local/bin/tsc; \
    ln -s ../lib/node_modules/tsx/dist/cli.mjs /usr/local/bin/tsx; \
    node -v && npm -v && tsc -v
COPY site /opt/site
# Pre-build the matplotlib font cache so the first chart is not slow.
RUN MPLCONFIGDIR=/opt/mpl python -c "import matplotlib.pyplot as plt; plt.figure()" && chmod -R a+rX /opt/mpl
COPY runner.py /opt/runner.py
COPY shell.sh /opt/shell.sh
RUN useradd -u 10001 -m sandbox && chmod 755 /opt/shell.sh
USER 10001
ENV MPLCONFIGDIR=/tmp/mpl
WORKDIR /work
# -I would drop PYTHONPATH, so isolate with -s -E-equivalent flags instead: no user site, no cwd on path.
ENTRYPOINT ["python", "-s", "-P", "/opt/runner.py"]
