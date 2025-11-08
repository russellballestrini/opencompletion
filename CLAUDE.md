# Claude Instructions

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

### Makefile Commands
- `make venv` - Create virtual environment and install dependencies
- `make init-db` - Initialize database tables
- `make test` - Run all tests
- `make dev-setup` - Install development dependencies

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

### Code Execution Integration (New)
- Integration with unfirecracker-code-executor service on cammy.foxhop.net
- Service supports 38 working languages with auto-detection
- Endpoints:
  - `/execute` - Execute code with specified language
  - `/run` - Auto-detect language and execute
- Add play button next to copy button for code blocks
- Display execution results inline below code blocks

## Activity YAML Schema

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
