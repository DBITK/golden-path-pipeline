# The demo workload as a container, for the Argo Rollouts manifests that
# `goldenpath export-argo` generates. The process executor in this repository
# runs app.py directly and does not need this image.
FROM python:3.12-slim

# No dependencies to install: the service is standard library only.
WORKDIR /app
COPY app.py .

# Run unprivileged. A deployment pipeline that ships root containers is not
# demonstrating good practice.
RUN useradd --create-home --uid 10001 app
USER app

ENV PORT=8080 \
    VERSION=0.0.0 \
    ROLE=baseline \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2).status==200 else 1)"

CMD ["python", "-u", "app.py"]
