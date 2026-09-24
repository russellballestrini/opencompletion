# OpenCompletion in a container: the same app `make venv` & `python app.py`
# run. Secrets never enter the image: mount vars.sh at run time (README.rst
# "Docker"); our SQLite database lives in instance/, mount it to keep it.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/server

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as a normal user; instance/ holds our SQLite database.
RUN useradd --create-home opencompletion \
    && mkdir -p instance \
    && chown -R opencompletion /opt/server
USER opencompletion

EXPOSE 5001

# Load vars.sh when mounted, create any missing tables, then serve.
CMD ["sh", "-c", "if [ -f vars.sh ]; then . ./vars.sh; fi; python init_db.py && exec python app.py"]
