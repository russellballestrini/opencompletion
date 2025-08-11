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
