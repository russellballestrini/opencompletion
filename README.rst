Open Completion
========================================

* repo: `opencompletion.com <https://opencompletion.com>`_

* demo: `demo.opencompletion.com <https://demo.opencompletion.com>`_

Chatroom applicationallows users to join rooms, send messages, & interact with multiple language models in real-time. Backend written with Flask & Flask-SocketIO for real-time web socket streaming. Frontend uses minimal HTML, CSS, & JavaScript to provide an interactive user interface.

Features
--------

- A home page that is an empty chat: type, & your first message starts a
  solo chat (only you, signed in) or a multiplayer chatroom anyone with the link can
  join. Its sidebar lists your chats & public ones, you at the bottom.
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
- Attach files to any message (picked, dropped or pasted), up to
  ``OPENCOMPLETION_UPLOAD_MAX_MB`` (default ``7``) each &
  ``OPENCOMPLETION_UPLOAD_LIMIT`` per address (default ``30/600``). Models
  read text & code files & see images (vision models). Bytes are stored
  once per content in SQLite (``blob_store.py``, tables ``blob`` &
  ``attachment``; run ``flask db upgrade`` or ``make init-db``).
- Speak instead of typing: the mic records in the browser & our server
  transcribes through ``SPEECH_ENDPOINT_n``'s ``/audio/transcriptions``
  (model ``SPEECH_STT_MODEL``, default ``whisper-1``;
  ``OPENCOMPLETION_TRANSCRIBE_LIMIT`` per address, default ``30/600``).
- A Stop button ends the reply streaming in a chat; what it wrote stays.
- Email OTP authentication with solo (only you) chat support
- Fork any model reply into a new chat holding the conversation up to it,
  limited per address (``OPENCOMPLETION_FORK_LIMIT``, default ``10/600``:
  10 forks per 10 minutes; ``0`` turns it off). Behind a proxy set
  ``OPENCOMPLETION_PROXY_HOPS`` to how many proxies front us (default ``1``,
  our Caddy; ``0`` uses the socket address).
- Edit & resend your own message: the edit replaces it & everything after it for
  everyone in the room & the models answer again. Nothing is deleted: the
  earlier branch is kept, hidden (run ``flask db upgrade``).
- A ⋯ menu on every chat (sidebar, Rooms, Search): rename, refresh title (a
  model titles it afresh), pin (kept in this browser), archive & delete
  (owner only).
- Follow-up suggestions under each reply, from the model that answered
  (``OPENCOMPLETION_FOLLOWUPS=off`` turns them off), and a ↓ button back to
  the newest message.
- Installable as an app on phones & desktops (``/manifest.webmanifest``,
  named by ``OPENCOMPLETION_BRAND``).
- Room archiving and owner management

Requirements
------------

- Python 3.11 or newer (CI runs 3.11, 3.12, 3.13 & 3.14)
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

   Chat replies sample at temperature ``OPENCOMPLETION_TEMPERATURE`` (0.6)
   & ``OPENCOMPLETION_TOP_P`` (0.95), never greedily (greedy decoding loops
   reasoning models), are capped at ``OPENCOMPLETION_MAX_TOKENS`` (8192;
   ``0`` = no cap) & stop early when a model keeps repeating itself
   (``OPENCOMPLETION_REPEAT_LIMIT``, 8 repeats; ``0`` = off).
   ``OPENCOMPLETION_PRESENCE_PENALTY`` is sent when set.

   ``OPENCOMPLETION_BRAND`` (optional) names the site in every header, for
   example ``uncloseai``; unset, it says OpenCompletion.

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

Two endpoints may serve one model: the lowest ``n`` listing it is its pin &
the others back it up, in order. A pin that is down (or fails while its
stream is opened) hands the reply to the next copy of the same model; only when
every copy is down does another model answer. A ``MODEL_n`` reference whose
endpoint is down looks for its model elsewhere the same way first.

Text to speech endpoints, configured like models (``n`` is any number, an
API key is optional). Our server proxies speech at ``/speech/voices`` and
``/speech/audio``, so keys never reach the browser; voices merge across
endpoints and a down endpoint falls through to the next one. Unset, speech
uses ``https://speech.ai.unturf.com/v1`` without a key::

    export SPEECH_ENDPOINT_1=https://speech.ai.unturf.com/v1
    export SPEECH_API_KEY_1=your-uncloseai-api-key

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
- ``/help``: Shows where things are (models, chats, room controls) & these commands.

Code Execution
--------------

Code blocks can be executed from chat with the "▶ Run" button. Supports 42+ programming languages with automatic language detection. Code runs in isolated, self-terminating Unsandbox containers, and compiled binaries can be downloaded from the interface.

Set ``UNSANDBOX_PUBLIC_KEY`` & ``UNSANDBOX_SECRET_KEY`` to enable it. Anyone may run code, guests included; each run spends that account, so ``OPENCOMPLETION_GUEST_CODE_EXEC=off`` keeps runs to signed-in people.


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
