Open Completion
========================================

* repo: `opencompletion.com <https://opencompletion.com>`_

* demo: `demo.opencompletion.com <https://demo.opencompletion.com>`_

Chatroom applicationallows users to join rooms, send messages, & interact with multiple language models in real-time. Backend written with Flask & Flask-SocketIO for real-time web socket streaming. Frontend uses minimal HTML, CSS, & JavaScript to provide an interactive user interface.

Features
--------

- Real-time messaging between users in a chatroom.
- Ability to join different chatrooms with unique URLs.
- Integration with language models for generating room titles and processing messages.
- Syntax highlighting for code blocks within messages.
- Markdown rendering for messages.
- **Code execution**: Run code blocks from chat in 42+ programming languages via Unsandbox.
- **Text-to-speech**: Read model replies aloud with multiple voice options.
- Commands to load and save code blocks to AWS S3.
- Database storage for messages and chatrooms using SQLAlchemy.
- Migration support with Flask-Migrate.
- Email OTP authentication with private room support
- Room forking, archiving, and owner management

Requirements
------------

- Python 3.11 or newer (CI runs 3.11 & 3.13)
- ``make``; everything else installs into ``venv/`` from ``requirements.txt``
- At least one OpenAI-compatible model endpoint (vLLM, Ollama, llama.cpp, ...)

Installation
------------

1. Clone this repository::

    git clone https://github.com/russellballestrini/opencompletion.git
    cd opencompletion

   **Git Remotes**: This repo is configured to push to both GitHub and unturf simultaneously.
   The ``origin`` remote has two push URLs:

   - GitHub: ``git@github.com:russellballestrini/opencompletion.git``
   - unturf: ``ssh://git@git.unturf.com:2222/engineering/unturf/opencompletion.com.git``

2. Create ``venv/`` with every dependency (re-run after a pull; it only
   reinstalls when a requirements file changed)::

    make venv

3. Copy ``vars.sh.sample`` to ``vars.sh`` & fill in your endpoints and keys
   (``vars.sh`` is gitignored; never commit it)::

    cp vars.sh.sample vars.sh

4. Create our database tables (a SQLite file in ``instance/`` unless
   ``SQLALCHEMY_DATABASE_URI`` says otherwise), then mark migrations current::

    make init-db
    source vars.sh && FLASK_APP=app venv/bin/flask db stamp head

5. Run it::

    source vars.sh && venv/bin/python app.py

Docker
------

Our ``Dockerfile`` runs the same app; ``vars.sh`` is mounted at run time,
never baked into an image::

    docker build -t opencompletion .
    docker run -p 5001:5001 \
        -v "$PWD/vars.sh:/opt/server/vars.sh:ro" \
        -v opencompletion-data:/opt/server/instance \
        opencompletion

The named volume keeps our SQLite database across containers (a host
directory would need to be writable by the container's non-root user). CI
builds & starts this image on every push (job ``docker``).

Tests
-----

``make test`` runs unit, integration & functional tests plus YAML validation
with no network; ``make test-ui`` checks every page in headless Chromium at
phone to desktop widths; ``make ci`` is exactly what GitHub Actions runs.

Usage
-----

Set up environment variables for your AWS, OpenAI, MistralAI, together.ai, grok, groq, google, API keys.

* make a copy of ``vars.sh.sample`` and fill in your API keys!

Other env vars::

    export AWS_ACCESS_KEY_ID="your_access_key"
    export AWS_SECRET_ACCESS_KEY="your_secret_key"
    export S3_BUCKET_NAME="your_s3_bucket_name"

Here are some free endpoint for research only!::

    export MODEL_ENDPOINT_1=https://hermes.ai.unturf.com/v1
    export MODEL_ENDPOINT_2=https://qwen.ai.unturf.com/v1
    export MODEL_ENDPOINT_3=https://gpt-oss.ai.unturf.com/v1

Optional classifier model, a decision endpoint that answers a categorization
AS a decision (a probability per bucket, nothing to parse). Asked first by
``categorize_response`` when configured, fails open onto the chat model.
Same vocabulary as ``uncloseai-cli`` and ``unhomeschool``; see ``classifier.py``
and ``make classifier-check``::

    export MODEL_CLASSIFIER_ENDPOINT_0=https://api.typesafe.ai
    export MODEL_CLASSIFIER_API_KEY_0=your-typesafe-api-key
    export MODEL_CLASSIFIER_ID_0=jev-latest        # optional
    export OPENCOMPLETION_CLASSIFIER=auto          # auto | on | off

Optional SMTP for email OTP authentication::

    export SMTP_HOST=smtp.gmail.com
    export SMTP_PORT=587
    export SMTP_USER=your@email.com
    export SMTP_PASSWORD=your_app_password

To start the application with socket.io run::

    python app.py

Optionally flags ``python app.py --local-activities --profile <aws-profile-name>``::

    usage: app.py [-h] [--profile PROFILE] [--local-activities] [--port PORT]

    options:
      -h, --help          show this help message and exit
      --profile PROFILE   AWS profile name
      --local-activities  Use local activity files instead of S3
      --port PORT         Port number (default: 5001)


The application will be available at ``http://127.0.0.1:5001`` by default.


Interacting with Language Models
--------------------------------

To interact with the various language models, choose from the drop down and send a message!

The system will process your message and provide a response from the selected language model.

Commands
--------

The chatrooms support some special commands:

- ``/title new``: Generates a new title which reflects conversation content for the current chatroom using our default model.
- ``/cancel``: Cancel the most recent chat completion from streaming into the chatroom.
- ``/help``: Displays the list of commands and models to choose from.

Code Execution
--------------

Code blocks can be executed from chat with the "▶ Run" button. Supports 42+ programming languages with automatic language detection. Code runs in isolated, self-terminating Unsandbox containers, and compiled binaries can be downloaded from the interface.

Set ``UNSANDBOX_PUBLIC_KEY`` & ``UNSANDBOX_SECRET_KEY`` to enable it. Running code spends that account, so it needs a signed-in person unless ``OPENCOMPLETION_GUEST_CODE_EXEC=on``.


Structure
---------

- ``app.py``: our Flask app & chat itself (the chat page, Socket.IO events, model streaming).
- ``routes/``: HTTP blueprints: pages, accounts (email-code sign in), rooms (browse, search, downloads), code execution.
- ``templates/``: pages extend ``layout.html``; chat extends ``base.html``. ``/styleguide`` shows every component.
- ``static/``: ``css/style.css`` (our one stylesheet), ``js/`` (site, sign in & ``chat/``), ``vendor/`` (pinned browser libraries).
- ``research/``: activity YAMLs & the ``guarded_ai.py`` simulator.
- ``docs/STYLEGUIDE.md``: our UI rules, tested by ``make test-ui``.


Activity Mode
--------------

Activity mode is an interactive experience where users learn & answer questions guided by machine learning.

Our model provides feedback based on the user's responses and guides them through different sections and steps of an activity.

This mode is designed to be on the "rails", educational, & engaging.

The server expects to load the YAML file out of the S3 bucket you specify in your environment variables.

1. **Start an Activity**: Use the ``/activity`` command followed by the object path to the activity YAML file to start a new activity.

    ``/activity path-to-activity.yaml``

2. **Display Activity Info**: Use the ``/activity info`` command to display information about the current activity, including grading and user performance.

    ``/activity info``

3. **Display Activity Metadata**: Use the ``/activity metadata`` command to display metadata information collected about the activity.

    ``/activity metadata``

4. **Cancel an Activity**: Use the ``/activity cancel`` command to display cancel the current activity running in the room.

    ``/activity cancel``


5. **Battleship example**:

    ``/activity research/activity29-battleship.yaml``

    .. image:: flask-socketio-llm-completions-battleship.png
        :align: center



Ollama versus vLLM
-----------------------------

We prefer operating an ``vllm`` inference server but some models are packaged exclusively for ``ollama`` so here is an example::

 ollama run hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:Q8_0

then::

 export MODEL_ENDPOINT_1=https://localhost:11434/v1

Then in the app you should be able to talk to ``NousResearch/Hermes-3-Llama-3.1-8B-GGUF:Q8_0``


Contributing
------------

Contributions to this project are welcome. Please follow the standard fork and pull request workflow.


License
-------

This project is public domain. It is free for use and distribution without any restrictions.


.. figure:: https://api.star-history.com/svg?repos=russellballestrini/opencompletion&type=Date
   :alt: Star History Chart
