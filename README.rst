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
- **Code execution**: Run code blocks directly in the browser with support for 38+ programming languages.
- **Text-to-speech**: Convert AI responses to speech with multiple voice options.
- Commands to load and save code blocks to AWS S3.
- Database storage for messages and chatrooms using SQLAlchemy.
- Migration support with Flask-Migrate.
- Email OTP authentication with private room support
- Room forking, archiving, and owner management

Requirements
------------

- Python 3.6+
- Flask
- Flask-SocketIO
- Flask-SQLAlchemy
- Flask-Migrate
- eventlet or gevent
- boto3 (for interacting with AWS Bedrock currently Claude, and S3 access)
- OpenAI client (for interacting with vLLM & Ollama inference servers)

Installation
------------

To set up the project, follow these steps:

1. Clone this repository::

    git clone https://github.com/russellballestrini/opencompletion.git
    cd opencompletion

2. Create a virtual environment and activate it::

    python3 -m venv env
    source env/bin/activate  # On Windows use `env\Scripts\activate`

3. Install the required dependencies::

    pip install -r requirements.txt

4. Initialize the database:

   Before running the application for the first time, you need to create the database and tables, and then stamp the Alembic migrations to mark them as up to date. Follow these steps::

        python init_db.py
        flask db stamp head

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

- ``/title new``: Generates a new title which reflects conversation content for the current chatroom using gpt-4.
- ``/cancel``: Cancel the most recent chat completion from streaming into the chatroom.
- ``/help``: Displays the list of commands and models to choose from.

Code Execution
--------------

Code blocks can be executed directly in the browser using the "▶ Run" button. Supports 30+ programming languages with automatic language detection. Code runs in isolated, self-terminating sandbox containers. Compiled binaries can be downloaded directly from the interface.


Structure
---------

- ``app.py``: The main Flask application file containing the backend logic.
- ``chat.html``: The HTML template for the chatroom interface.
- ``static/``: Directory for static files like CSS, JavaScript, and images.
- ``templates/``: Directory for HTML templates.
- ``research/``: Guarded AI activities or processes. Example YAMLs.


Activity Mode
--------------

Activity mode is an interactive experience where users can engage with a guided AI to learn and answer questions.

The AI provides feedback based on the user's responses and guides them through different sections and steps of an activity.

This mode is designed to be on the "rails", educational, & engaging.

The server expects to load the YAML file out of the S3 bucket you specify in your environment variables.

1. **Start an Activity**: Use the ``/activity`` command followed by the object path to the activity YAML file to start a new activity.

    ``/activity path-to-activity.yaml``

2. **Display Activity Info**: Use the ``/activity info`` command to display AI information about the current activity, including grading and user performance.

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
