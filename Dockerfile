FROM python:3.12-slim

WORKDIR /srv

# The application core is standard library only. reportlab is the single
# optional dependency, used to render sample invoice PDFs for the upload
# path; the app degrades gracefully if it is missing.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY README.md ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# Fail the build if the match engine is broken.
RUN python3 -m tests

# A model key (ANTHROPIC_API_KEY or OPENROUTER_API_KEY) is optional. Without
# one, everything works except reading uploaded PDFs and running the
# investigation agent; retrieval falls back to BM25 alone. The UI says which,
# rather than failing silently when someone uses it.
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
  CMD python3 -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health',timeout=3)"

CMD ["python3", "-m", "app.cli", "serve"]
