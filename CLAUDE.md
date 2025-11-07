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
