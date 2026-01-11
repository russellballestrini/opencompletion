# Claude Instructions

## Git Remotes

This repository has multiple push targets configured on the `origin` remote:
- **GitHub**: `git@github.com:russellballestrini/opencompletion.git` (fetch & push)
- **Unturf**: `ssh://git@git.unturf.com:2222/engineering/unturf/opencompletion.com.git` (push only)

When you `git push origin main`, changes are pushed to both remotes simultaneously.

To verify remote configuration:
```bash
git remote -v
```

## Commit Messages
- NEVER add Claude attributions like "🤖 Generated with Claude Code" to commit messages
- NEVER add "Co-Authored-By: Claude <noreply@anthropic.com>" to commit messages
- Keep commit messages focused on the actual changes and their purpose
- Use conventional commit format when appropriate
- Be concise but descriptive about what was changed and why

## Code Style
- Follow existing code conventions in the project
- Use appropriate linting tools (black, ruff, etc.) when available
- Maintain consistent naming and formatting

## Testing
- Run existing tests before committing when available
- Write tests for new functionality when appropriate
- Verify changes work as expected

## Linting
- **ALWAYS run lint before committing**: `make lint` or `flake8 app.py activity.py --select=E9,F63,F7,F82`
- Fix all lint errors before pushing - GitHub CI will fail on lint errors
- Key error codes checked:
  - E9: Runtime errors (syntax errors, IO errors)
  - F63: Invalid print syntax
  - F7: Syntax errors in type comments
  - F82: Undefined names, unused globals (F824)

## Documentation
- Update relevant documentation when making significant changes
- Keep README files current with new features or setup changes
- Document any new environment variables or configuration options

## Python/Matplotlib Best Practices
- Always add `matplotlib.use("Agg")` before importing matplotlib.pyplot to prevent runtime errors in headless environments

## Makefile Best Practices
- Avoid variable substitutions - don't be afraid to be unDRY in the Makefile so engineers can copy and paste
- Use tabs not spaces, and for fuck sake be happy about it

## Running OpenCompletion

### Environment Setup
- Use `vars.sh` to set up environment variables
- Required: MODEL_ENDPOINT_x and MODEL_API_KEY_x variables for AI models
- Run with: `source vars.sh && python app.py`
- **NEVER cat or grep vars.sh** - it contains API keys and secrets

### Makefile Commands
- `make venv` - Create virtual environment and install dependencies
- `make init-db` - Initialize database tables
- `make test` - Run all tests
- `make lint` - Run code linting (black, isort, flake8)
- `make dev-setup` - Install development dependencies

### Network Infrastructure

- OpenCompletion uses Caddy for web server (not nginx)
- Multi-layer proxy architecture for accessing AI models
- See `unturf-debugging.md` for network troubleshooting (gitignored)

## OpenCompletion Architecture

### Frontend Structure
- Main chat interface is in `templates/chat.html`
- Base template with CSS is in `templates/base.html`
- JavaScript code is inline in chat.html for real-time chat functionality
- Uses Socket.IO for WebSocket communication
- Uses marked.js for Markdown rendering and DOMPurify for XSS protection
- Code blocks are rendered with highlight.js for syntax highlighting

### Code Block Rendering
- Code blocks are processed in messages after markdown conversion
- Copy buttons are added via `addCopyButtonToCodeBlock()` function (line 1176 in chat.html)
- Code blocks support:
  - Syntax highlighting via highlight.js
  - Line numbers via `addLineNumbers()` function
  - Truncation for long code blocks via `truncateCodeBlock()` function
  - Copy functionality that preserves full content even when truncated

### Message Processing Flow
1. Messages received via Socket.IO events (chat_message, message_chunk for streaming)
2. Markdown converted to HTML using marked.js
3. HTML sanitized with DOMPurify
4. Code blocks enhanced with copy buttons, syntax highlighting, and line numbers

### Code Execution Integration

OpenCompletion integrates with the Unsandbox API (https://api.unsandbox.com) for secure code execution in 40+ programming languages.

#### API Endpoints

**Synchronous Execution** (immediate results):
```
POST https://api.unsandbox.com/execute
```
- Executes code immediately and returns results
- Use for quick code snippets and interactive execution

**Asynchronous Execution** (long-running tasks):
```
POST https://api.unsandbox.com/execute/async
```
- Returns job ID for later retrieval
- Use for long-running scripts (up to 15 minutes)

**Auto-Detect Language**:
```
POST https://api.unsandbox.com/run
```
- Automatically detects language from shebang
- Send raw code as request body
- Useful when language is unknown or embedded in script

#### Request Format

```json
{
  "language": "python",
  "code": "print('Hello, World!')",
  "env": {
    "VAR_NAME": "value"
  },
  "network_mode": "zerotrust",
  "ttl": 60
}
```

**Parameters**:
- `language` (required): Programming language identifier
- `code` (required): Source code to execute
- `env` (optional): Environment variables as key-value pairs
- `network_mode` (optional): "zerotrust" (default) or "semitrusted"
- `ttl` (optional): Timeout in seconds (1-900, default 60)

#### Response Format

**Success Response**:
```json
{
  "success": true,
  "stdout": "Hello, World!\n",
  "stderr": "",
  "exit_code": 0
}
```

**Error Response**:
```json
{
  "success": false,
  "stdout": "",
  "stderr": "SyntaxError: invalid syntax\n",
  "exit_code": 1,
  "error": "Runtime error occurred"
}
```

**Response Fields**:
- `success` (boolean): True if execution completed without errors
- `stdout` (string): Standard output from the program
- `stderr` (string): Standard error output
- `exit_code` (integer): Program exit status (0 = success, non-zero = error)
- `error` (string, optional): Detailed error message if execution failed
- `detected_language` (string, optional): Language detected by auto-detect endpoint

#### Authentication

Uses HMAC-SHA256 authentication with public/secret key pairs:

**Environment Variables:**
- `UNSANDBOX_PUBLIC_KEY` - Public key (unsb-pk-xxxx) used as Bearer token to identify account
- `UNSANDBOX_SECRET_KEY` - Secret key (unsb-sk-xxxx) used for HMAC signing, never transmitted

**Request Headers:**
```
Authorization: Bearer <public_key>
X-Timestamp: <unix_seconds>
X-Signature: HMAC-SHA256(secret_key, timestamp:method:path:body)
```

The secret key is never transmitted - server verifies HMAC using its stored copy.
Timestamp must be within ±5 minutes of server time (replay attack prevention).

#### Supported Languages

40+ languages including:
- **Compiled**: C, C++, Rust, Go, Java, C#, Swift
- **Interpreted**: Python, Ruby, JavaScript, PHP, Perl, Lua
- **Scripting**: Bash, PowerShell, Fish
- **Data**: R, Julia, Octave, MATLAB
- **Functional**: Haskell, Scala, Erlang, Elixir
- **Esoteric**: Brainfuck, LOLCODE
- And many more...

#### Frontend Integration

- Add play button (▶) next to copy button on code blocks
- Execute code when user clicks play button
- Display execution results inline below code block
- Show stdout, stderr, and exit_code separately
- Use syntax highlighting for output
- Handle timeouts gracefully (60s default)
- Support language auto-detection for fenced code blocks

#### Security Features

- **Isolated Execution**: Each execution runs in isolated container
- **Network Control**: Zero-trust or semi-trusted network modes
- **Timeout Protection**: Automatic termination after TTL expires
- **Resource Limits**: CPU, memory, and disk quotas enforced
- **Safe Defaults**: Minimal privileges, read-only filesystem (except /tmp)

## Activity YAML Schema

### Session Persistence & Multi-User Model ("Twitch Plays Pokemon")

**How OpenCompletion Activities Work:**

- **Single Shared Game State**: One activity instance per room/channel
- **Multiple Players**: Zero or more users can participate from different devices
- **Collaborative Control**: Any user can provide input to advance the shared game
- **Persistent Metadata**: State is stored in the database per-room, survives browser refreshes
- **Like "Twitch Plays Pokemon"**: Everyone sees the same state, anyone can control

**Key Implications:**
- `metadata` is **shared** across all users in the room - it's the game state, not player-specific
- When user "Alice" adds metadata, user "Bob" sees it too (same activity instance)
- Use metadata for: scores, progress, choices, inventory, flags - anything that's part of the game
- All users see the same content_blocks, questions, and transitions
- Multiple users can answer the same question - first valid answer advances the game
- Activities can be canceled, which deletes the room's activity state

**Session Lifecycle:**
1. Activity starts → Initial state saved to database (room_id, section_id, step_id, metadata)
2. Users interact → Metadata updates, state progresses through sections/steps
3. Activity completes → State deleted from database
4. Activity canceled → State deleted from database

**Use Cases:**
- Classroom activities where teacher projects screen, students call out answers
- Collaborative puzzles where multiple people work together
- Public challenges where community collectively progresses
- Educational games where everyone learns from same shared experience

### Model Configuration (New Feature)

Activities can specify separate models for classification and feedback generation:

```yaml
# Activity-level defaults (optional)
classifier_model: "MODEL_1"  # For categorizing user responses into buckets
feedback_model: "MODEL_1"    # For generating AI feedback and translations

# Step-level overrides (optional)
sections:
  - section_id: "coding"
    steps:
      - step_id: "code_review"
        classifier_model: "MODEL_1"  # Keep fast classification
        feedback_model: "MODEL_3"    # Use specialized code model
```

**Why Separate Models?**

1. **Speed**: Use fast 8B models for classification → instant bucketing
2. **Quality**: Use specialized models for feedback → better explanations
3. **Cost Efficiency**: Don't waste tokens on simple categorization
4. **Flexibility**: Override per-step for specific needs

**Model Defaults**

If not specified, both default to `MODEL_1` (Hermes-3-Llama-3.1-8B):
- Always available in base install
- Fast and accurate
- Excellent for role-playing and general tasks
- Great classifier and feedback generator

**Recommended Model Combinations**

| Activity Type | Classifier | Feedback | Rationale |
|--------------|------------|----------|-----------|
| General Education | MODEL_1 | MODEL_1 | Fast, accurate, always available |
| Programming | MODEL_1 | MODEL_3 | Fast bucketing + code specialist (Qwen3-Coder) |
| Role-Playing | MODEL_1 | MODEL_1 | Hermes excels at character consistency |
| Advanced Topics | MODEL_1 | MODEL_2 | Fast bucketing + larger model for depth |

**Environment Variables**

Models are configured via environment variables in `vars.sh`:

```bash
# MODEL_1 - Hermes (always available, default)
export MODEL_ENDPOINT_1=http://localhost:8080/v1
export MODEL_API_KEY_1=your-api-key
export MODEL_NAME_1=model  # Optional: actual model name for the endpoint

# MODEL_2 - Additional model (optional)
export MODEL_ENDPOINT_2=http://localhost:8081/v1
export MODEL_API_KEY_2=your-api-key
export MODEL_NAME_2=gpt-4  # Optional: specify deployment/model name

# MODEL_3 - Qwen3-Coder (recommended for programming)
export MODEL_ENDPOINT_3=http://localhost:8082/v1
export MODEL_API_KEY_3=your-api-key
export MODEL_NAME_3=model  # Optional: defaults to "model" if not specified
```

**Note**: `MODEL_NAME_{n}` is optional and defaults to `"model"`. Some endpoints (like Azure OpenAI) require the actual deployment name - set this variable for those cases.

**Example: Programming Activity**

```yaml
# research/activity37-programming-languages.yaml
classifier_model: "MODEL_1"  # Hermes for fast classification
feedback_model: "MODEL_3"    # Qwen3-Coder-30B for code generation

sections:
  - section_id: "hello_world"
    steps:
      - step_id: "write_hello"
        question: "Write a Hello World program in your chosen language"
        tokens_for_ai: |
          Get the student's chosen language from metadata (programming_language).
          Evaluate their code in THAT specific language.
        feedback_tokens_for_ai: |
          Provide detailed feedback on their code syntax and style.
          Generate example code if they need help.
```

### Activity YAML Validation

**Validator Location**: `activity_yaml_validator.py`

**Validate Activities**:
```bash
python activity_yaml_validator.py research/activity*.yaml
```

**Model Field Validation**:
- `classifier_model` (optional, string): Activity or step-level
- `feedback_model` (optional, string): Activity or step-level
- Both default to "MODEL_1" if not specified
- Can reference MODEL_1, MODEL_2, MODEL_3, etc.

**Testing Activities**

CLI simulation tool supports model configuration:

```bash
source vars.sh
python research/guarded_ai.py research/activity37-programming-languages.yaml
# Uses MODEL_1 for classification, MODEL_3 for code feedback
```

### Model Setup: Qwen3-Coder-30B (MODEL_3)

**Why Qwen3-Coder?**
- 30B parameters (much smarter for code)
- Trained on 100+ programming languages
- Q4_K_M quantization (~20GB RAM)
- Perfect for activity37 (universal programming activity)

**Setup with llama.cpp**:
```bash
# Download
huggingface-cli download unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF \
  Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf

# Run server (GPU acceleration)
llama-server -m Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8082 -ngl 99

# Configure in vars.sh
export MODEL_ENDPOINT_3=http://localhost:8082/v1
export MODEL_API_KEY_3=dummy
```

**Setup with ollama**:
```bash
ollama run unsloth/qwen3-coder:30b-instruct-q4_K_M

# Configure in vars.sh
export MODEL_ENDPOINT_3=http://localhost:11434/v1
export MODEL_API_KEY_3=dummy
```

## Creating Activity YAML Files - Expert Guide

**IMPORTANT: Before creating or modifying any activity YAML files:**
1. **ALWAYS read `research/SPEC.yaml` first** to ensure you have the latest specification and examples
2. **ALWAYS validate the YAML after creating/modifying** by running:
   ```bash
   python activity_yaml_validator.py research/your_activity.yaml
   ```
3. **All activity YAMLs MUST pass validation** with 0 errors before committing

When creating activities for OpenCompletion, follow these expert guidelines to ensure your activities **validate properly**, are **FUN and engaging**, and **terminate correctly**.

### Core Activity Structure

Every activity YAML file consists of:

```yaml
# Optional: Global settings
default_max_attempts_per_step: 3  # Default retry limit
classifier_model: "MODEL_1"       # Model for categorizing responses
feedback_model: "MODEL_1"         # Model for generating feedback
tokens_for_ai_rubric: |           # Global rubric for all steps
  Evaluate the student's understanding...

# Required: Sections contain steps
sections:
  - section_id: "introduction"    # Must be unique
    title: "Welcome"              # Descriptive title
    steps:
      - step_id: "welcome"        # Must be unique within section
        title: "Getting Started"
        # Either content_blocks OR question (or both)
        content_blocks:           # Display-only content
          - "Welcome message"
        question: "Ready?"        # Interactive question
        buckets: [ready, not_ready]  # Response categories
        transitions:              # One per bucket
          ready:
            next_section_and_step: "section_1:step_1"
```

**Two Types of Steps:**

1. **Content-Only Steps** - Display information, automatically advance
   ```yaml
   - step_id: "info"
     title: "Information"
     content_blocks:
       - "This is informational content."
       - "It displays and auto-advances."
   ```

2. **Question Steps** - Interactive, require user response
   ```yaml
   - step_id: "quiz"
     title: "Question"
     question: "What is 2+2?"
     tokens_for_ai: |
       Categorize as 'correct' if answer is 4 or 'four'.
       Otherwise 'incorrect'.
     buckets: [correct, incorrect]
     transitions:
       correct:
         content_blocks: ["Great job!"]
         next_section_and_step: "next_section:next_step"
       incorrect:
         content_blocks: ["Try again!"]
         next_section_and_step: "quiz_section:quiz"
   ```

### CRITICAL: Validation Requirements

**MUST-PASS Checklist** (from activity_yaml_validator.py):

#### Structure Requirements
- ✅ **Every activity must have `sections`** (at least one)
- ✅ **Every section needs**: `section_id`, `title`, `steps`
- ✅ **Every step needs**: `step_id`, `title`, and either `content_blocks` OR `question`
- ✅ **Section IDs must be unique** within the activity
- ✅ **Step IDs must be unique** within each section

#### Bucket & Transition Requirements
- ✅ **Every bucket MUST have a corresponding transition** (CRITICAL!)
  ```yaml
  # WRONG - Missing transition for 'maybe' bucket
  buckets: [yes, no, maybe]
  transitions:
    yes: {...}
    no: {...}
    # ❌ ERROR: No transition for 'maybe'

  # CORRECT - All buckets have transitions
  buckets: [yes, no, maybe]
  transitions:
    yes: {...}
    no: {...}
    maybe: {...}  # ✅ Every bucket covered
  ```

#### Termination Requirements
- ✅ **Terminal steps (last step of last section with no next_section_and_step) CANNOT have questions**
  ```yaml
  # WRONG - Terminal step with question
  - section_id: "conclusion"
    steps:
      - step_id: "final"
        question: "How did you like it?"  # ❌ ERROR
        buckets: [good, bad]
        transitions:
          good: {}  # No next_section_and_step = terminal
          bad: {}

  # CORRECT - Terminal step with content only
  - section_id: "conclusion"
    steps:
      - step_id: "final"
        title: "Goodbye"
        content_blocks:  # ✅ Content only
          - "Thank you for playing!"
  ```

#### Transition Target Requirements
- ✅ **All `next_section_and_step` targets must exist**
  ```yaml
  # Format: "section_id:step_id"
  next_section_and_step: "section_2:step_1"  # Must exist!
  ```

#### Python Code Requirements
- ✅ **All `processing_script` and `pre_script` must be syntactically valid Python**
  ```yaml
  # CORRECT
  processing_script: |
    result = user_input.lower()
    metadata['guess'] = result

  # WRONG - Syntax error
  processing_script: |
    result = user_input.lower(  # ❌ Missing closing paren
  ```

#### Model Configuration (Optional)
- ✅ **`classifier_model` and `feedback_model` must be strings if specified**
  ```yaml
  classifier_model: "MODEL_1"  # ✅ Correct
  feedback_model: MODEL_1       # ❌ Wrong (unquoted)
  ```

### How to Properly Terminate Activities

Activities can terminate in four ways:

#### 1. Content-Only Terminal Step (Simplest)
Last step of last section has only `content_blocks`, no question:
```yaml
sections:
  - section_id: "conclusion"
    steps:
      - step_id: "goodbye"
        title: "Farewell"
        content_blocks:
          - "Thank you for playing! 🎉"
          - "Come back anytime!"
        # No question = auto-terminates
```

#### 2. Final Reflection Question (Educational Activities)
Last step has question, but NO transitions specify `next_section_and_step`:
```yaml
sections:
  - section_id: "conclusion"
    steps:
      - step_id: "reflection"
        title: "Final Thoughts"
        question: "What did you learn today?"
        tokens_for_ai: "Provide encouraging feedback on their reflection."
        buckets: [thoughtful, brief, off_topic]
        transitions:
          thoughtful:
            ai_feedback:
              tokens_for_ai: "Celebrate their learning!"
            metadata_add:
              activity_completed: "true"
            # No next_section_and_step = terminates
          brief:
            ai_feedback:
              tokens_for_ai: "Thank them for their time."
            metadata_add:
              activity_completed: "true"
          off_topic:
            content_blocks:
              - "Please reflect on what you learned."
            next_section_and_step: "conclusion:reflection"  # Retry
```

#### 3. Explicit Exit Transition (Games/Interactive)
Create an 'exit' bucket that leads to a goodbye step:
```yaml
- step_id: "play_again"
  question: "Would you like to play again?"
  buckets: [yes, exit]
  transitions:
    yes:
      metadata_clear: true  # Reset game state
      next_section_and_step: "game:start"
    exit:
      next_section_and_step: "conclusion:goodbye"  # Jump to end
```

#### 4. Max Attempts Exhausted (Automatic Fallback)
After 3 failed attempts (default), system auto-advances:
```yaml
default_max_attempts_per_step: 3

# After 3 attempts, automatically moves to next step
# Use counts_as_attempt: false for transitions that shouldn't count
transitions:
  correct:
    next_section_and_step: "next:step"
  hint:
    content_blocks: ["Here's a hint..."]
    counts_as_attempt: false  # Doesn't count toward max
    next_section_and_step: "current:step"  # Retry
  incorrect:
    content_blocks: ["Try again!"]
    next_section_and_step: "current:step"  # Retry (counts)
```

**CRITICAL Termination Rule**: Use `metadata_add: activity_completed: "true"` in your final transitions to mark completion!

### What Makes Activities FUN and Engaging

Study activity26-magic-8-ball.yaml, activity31-scientific-method.yaml, and activity37-programming-languages.yaml for examples.

#### 1. **Looping/Replayability**
Allow users to repeat fun parts:
```yaml
# Magic 8 Ball - loops back to itself
transitions:
  ask_question:
    ai_feedback: {...}
    next_section_and_step: "section_1:step_1"  # Loop!
  exit:
    next_section_and_step: "section_1:goodbye"
```

#### 2. **Randomness & Variety**
Use `metadata_tmp_random` or `metadata_random` for unpredictability:
```yaml
transitions:
  roll_dice:
    metadata_tmp_random:
      dice_result: [1, 2, 3, 4, 5, 6]  # Random pick
    ai_feedback:
      tokens_for_ai: |
        The dice roll is in metadata.dice_result.
        Announce it dramatically! 🎲
```

#### 3. **Personalization with Metadata**
Store and reference user choices throughout:
```yaml
# Step 1: Store user's name
transitions:
  greeting:
    metadata_add:
      player_name: "the-users-response"

# Step 5: Reference their name
tokens_for_ai: |
  Address the user by their name from metadata.player_name.
  Make it personal!
```

#### 4. **AI Personality & Encouragement**
Make the AI engaging:
```yaml
ai_feedback:
  tokens_for_ai: |
    Be enthusiastic! Use emojis! 🎉
    Celebrate their success with a joke related to their answer.
    On a new line, encourage them to continue.
```

#### 5. **Progressive Scoring**
Track and display progress:
```yaml
metadata_add:
  score: "n+1"  # Increment score
  correct_answers: "n+1"

# In final step
content_blocks:
  - "Your final score: check metadata.score"
  - "You got metadata.correct_answers correct!"
```

#### 6. **Multiple Valid Paths**
Different quality responses get different feedback:
```yaml
buckets:
  - excellent_answer    # Perfect understanding
  - correct_answer      # Got it right
  - partial_understanding  # On the right track
  - creative_thinking   # Wrong but interesting
  - needs_help          # Need more guidance
  - off_topic          # Completely off

# Each bucket gets tailored feedback and appropriate next step
```

#### 7. **Visual Variety & Formatting**
Use markdown, emojis, and structure:
```yaml
content_blocks:
  - "# Welcome to the Adventure! 🗺️"
  - "You stand at a crossroads..."
  - ""
  - "**North**: A dark forest 🌲"
  - "**South**: A sunny beach 🏖️"
  - "**East**: A mysterious cave 🕳️"
  - ""
  - "Where will you go?"
```

#### 8. **Educational Scaffolding**
Build complexity gradually:
```yaml
# Section 1: Simple concepts with lots of support
# Section 2: Intermediate - less hand-holding
# Section 3: Advanced - challenging applications
# Section 4: Reflection and synthesis
```

#### 9. **Role-Playing & Storytelling**
Create engaging narratives:
```yaml
tokens_for_ai: |
  You are a wise wizard guiding the student.
  Stay in character! Speak mysteriously.
  Reference their previous choices from metadata.
```

#### 10. **Immediate, Specific Feedback**
Don't just say "correct" or "wrong":
```yaml
feedback_tokens_for_ai: |
  If they identified the scientific method correctly:
  - Praise the specific insight they showed
  - Connect it to real-world applications
  - Encourage them to apply this thinking

  If they struggled:
  - Acknowledge what they got right first
  - Gently correct the misunderstanding
  - Provide a hint or example
  - Encourage them to try again
```

### Best Practices for Activity Creation

1. **Start with the Learning Goals**
   - What should the user know/be able to do after completion?
   - Design backwards from those outcomes

2. **Write Clear AI Instructions**
   ```yaml
   # VAGUE - AI won't know what to do
   tokens_for_ai: "Check if they understand."

   # SPECIFIC - AI knows exactly what to do
   tokens_for_ai: |
     Categorize as 'correct' if they mention:
     - Variables store data
     - Types define what kind of data
     - Examples: strings, numbers, booleans

     Categorize as 'partial' if they only mention one aspect.
     Categorize as 'incorrect' otherwise.
   ```

3. **Design Metadata Strategically**
   - Store meaningful state that affects the experience
   - Don't track everything - only what you'll reference
   - Use descriptive key names: `programming_language` not `pl`

4. **Test All Paths**
   ```bash
   # Use the CLI simulator
   source vars.sh
   python research/guarded_ai.py research/your_activity.yaml

   # Try:
   # - Correct answers
   # - Wrong answers
   # - Edge cases
   # - Max attempts exhaustion
   # - Language switching
   # - All branches/sections
   ```

5. **Validate Early and Often**
   ```bash
   python activity_yaml_validator.py research/your_activity.yaml
   ```

6. **Use Comments Liberally**
   ```yaml
   # This section teaches variables
   # User's chosen language is in metadata.programming_language
   - section_id: "variables"
     steps:
       # First, explain what variables are
       - step_id: "explain"
         # ... then quiz them
       - step_id: "quiz"
   ```

7. **Provide Multiple Difficulty Paths**
   ```yaml
   # Allow users to request hints
   buckets: [correct, incorrect, need_hint]
   transitions:
     need_hint:
       content_blocks: ["Hint: Think about..."]
       counts_as_attempt: false
       next_section_and_step: "current:question"  # Retry
   ```

8. **Support Language Switching**
   Always include a `set_language` bucket:
   ```yaml
   buckets: [answer, set_language, off_topic]
   transitions:
     set_language:
       content_blocks:
         - "Language preference updated."
       metadata_add:
         language: "the-users-response"
       counts_as_attempt: false
       next_section_and_step: "current:step"  # Retry in new language
   ```

9. **Write Engaging Content Blocks**
   ```yaml
   # BORING
   content_blocks:
     - "This is about variables."

   # ENGAGING
   content_blocks:
     - "# Let's Talk About Variables! 📦"
     - "Imagine your computer's memory as a huge warehouse..."
     - "Variables are like labeled boxes where you store information."
     - ""
     - "**Why do we need them?** Without variables, programs can't remember anything!"
   ```

10. **Design for Replayability**
    - Use randomness for variety
    - Support restart/retry paths
    - Allow skipping to different sections
    - Make it fun to play multiple times

### Common Pitfalls to AVOID

| Pitfall | Why It Fails Validation | How to Fix |
|---------|------------------------|------------|
| **Missing transition for a bucket** | Every bucket MUST have a transition | Add transition for ALL buckets |
| **Terminal step with question** | Last step of last section cannot have questions/buckets | Make final step content-only |
| **Circular loop without exit** | Users get trapped, max_attempts saves them but feels bad | Always provide an 'exit' bucket or progression path |
| **Invalid transition target** | References non-existent section:step | Verify all targets exist: `python activity_yaml_validator.py` |
| **Python syntax errors in scripts** | Crashes at runtime | Test your Python code before adding to YAML |
| **Vague AI instructions** | AI categorizes incorrectly, wrong buckets | Be specific about what makes each bucket |
| **Boolean values as strings** | `"true"` is a string, not boolean | Use `true/false` not `"true"/"false"` |
| **Forgetting `counts_as_attempt: false`** | Hints/language changes count as failures | Add `counts_as_attempt: false` to helper transitions |
| **No activity_completed marker** | Can't track completion | Add `metadata_add: activity_completed: "true"` to final transitions |
| **Inconsistent metadata keys** | `score` vs `Score` vs `total_score` | Pick one naming scheme and stick to it |
| **Too many attempts before feedback** | Users get frustrated | Default to 3 max, provide hints after attempt 1 |
| **Generic feedback** | "Good job!" isn't helpful | Reference specific parts of their answer |
| **Dead-end paths** | User stuck, can't progress | Always provide a way forward (even if it's restarting) |
| **Ignoring the rubric** | Global `tokens_for_ai_rubric` tells AI how to evaluate | Define it for consistency across steps |
| **Showing answers before questions** | Users copy-paste instead of learning | Explain CONCEPTS in content_blocks, provide CODE EXAMPLES only in ai_feedback |

### Activity Development Workflow

1. **Plan Structure**
   - Sketch sections and learning progression
   - Identify key decision points
   - Map out metadata usage

2. **Write YAML**
   - Start with one section
   - Test it in the simulator
   - Expand incrementally

3. **Validate**
   ```bash
   python activity_yaml_validator.py research/your_activity.yaml
   ```

4. **Test Interactively**
   ```bash
   source vars.sh
   python research/guarded_ai.py research/your_activity.yaml
   ```

5. **Test All Paths**
   - Try every bucket
   - Exhaust max attempts
   - Test edge cases
   - Verify termination

6. **Refine**
   - Improve AI instructions based on testing
   - Adjust bucket categories
   - Polish content blocks
   - Add variety and engagement

7. **Final Validation**
   - Run validator one more time
   - Test complete playthrough
   - Verify all transitions work
   - Confirm proper termination

### Quick Reference: Essential Fields

```yaml
# Activity Level (Root)
default_max_attempts_per_step: 3           # Optional, defaults to 3
classifier_model: "MODEL_1"                # Optional, defaults to MODEL_1
feedback_model: "MODEL_1"                  # Optional, defaults to MODEL_1
tokens_for_ai_rubric: "..."               # Optional global rubric
sections: [...]                            # REQUIRED

# Section Level
section_id: "unique_id"                    # REQUIRED, unique
title: "Section Title"                     # REQUIRED
steps: [...]                               # REQUIRED

# Step Level (Content-Only)
step_id: "unique_id"                       # REQUIRED, unique in section
title: "Step Title"                        # REQUIRED
content_blocks: [...]                      # REQUIRED (if no question)

# Step Level (Question)
step_id: "unique_id"                       # REQUIRED
title: "Step Title"                        # REQUIRED
question: "Your question?"                 # REQUIRED (if no content_blocks)
tokens_for_ai: "Categorization rules"      # Recommended
feedback_tokens_for_ai: "Feedback rules"   # Recommended
buckets: [...]                             # REQUIRED (with question)
transitions: {...}                         # REQUIRED (with buckets)
classifier_model: "MODEL_1"                # Optional step-level override
feedback_model: "MODEL_1"                  # Optional step-level override

# Transition Level
next_section_and_step: "section:step"      # Optional (omit to terminate)
content_blocks: [...]                      # Optional static feedback
ai_feedback:                               # Optional AI-generated feedback
  tokens_for_ai: "..."                     # Prompt for feedback
metadata_add: {key: "value"}               # Add/update metadata
metadata_tmp_add: {key: "value"}           # Temporary metadata (one turn)
metadata_random: {key: [...]}              # Add random value from list
metadata_tmp_random: {key: [...]}          # Temporary random value
metadata_remove: "key" or ["key1", "key2"] # Remove metadata keys
metadata_clear: true                       # Clear all metadata
metadata_feedback_filter: ["key1", "key2"] # Filter feedback by metadata
counts_as_attempt: false                   # Don't count toward max_attempts
run_processing_script: true                # Execute step's processing_script
```

### Example: Complete Minimal Activity

```yaml
default_max_attempts_per_step: 3
sections:
  - section_id: "intro"
    title: "Introduction"
    steps:
      - step_id: "welcome"
        title: "Welcome"
        content_blocks:
          - "# Welcome to Math Quiz! 🔢"
          - "Let's test your addition skills!"

      - step_id: "quiz"
        title: "Addition Question"
        question: "What is 5 + 7?"
        tokens_for_ai: |
          Categorize as 'correct' if they answer 12 or "twelve".
          Categorize as 'close' if they're within 2 (10, 11, 13, 14).
          Otherwise 'incorrect'.
        buckets: [correct, close, incorrect]
        transitions:
          correct:
            content_blocks:
              - "Perfect! 🎉"
            metadata_add:
              score: "n+1"
            next_section_and_step: "conclusion:goodbye"
          close:
            content_blocks:
              - "Close! Think again."
            next_section_and_step: "intro:quiz"
          incorrect:
            content_blocks:
              - "Not quite. Try adding 5 + 7 again."
            next_section_and_step: "intro:quiz"

  - section_id: "conclusion"
    title: "Conclusion"
    steps:
      - step_id: "goodbye"
        title: "Goodbye"
        content_blocks:
          - "Thanks for playing! 👋"
```

This activity:
- ✅ Validates (all required fields present)
- ✅ Is fun (emoji, encouraging feedback, score tracking)
- ✅ Terminates properly (content-only final step)

**Now you're ready to create amazing activities!** 🚀
