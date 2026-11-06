#!/bin/sh

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d " " -f 2)
MAJOR_VERSION=$(echo "$PYTHON_VERSION" | cut -d "." -f 1)
MINOR_VERSION=$(echo "$PYTHON_VERSION" | cut -d "." -f 2)

if [ "$MAJOR_VERSION" -lt 3 ] || { [ "$MAJOR_VERSION" -eq 3 ] && [ "$MINOR_VERSION" -lt 11 ]; }; then
  echo "Error: Python version 3.11 or greater is required."
  echo "Found Python version $PYTHON_VERSION"
  exit 1
fi

echo "Python version check passed ($PYTHON_VERSION)."

# Setup Git hooks path to use .githooks directory
echo "Configuring git hooks..."
git config core.hooksPath .githooks

echo "Git hooks path set to .githooks/"
echo "✅ Pre-commit hook is now active."

# Install uv if not present
if ! command -v uv > /dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  echo "Please restart your shell or run: source $HOME/.cargo/env"
  exit 0
fi

# Install dependencies using uv (including dev tools for formatting)
echo "Installing dependencies with uv..."
uv sync --extra dev

echo "Checking pipecat version..."
uv run python -c "import pipecat; print(f'pipecat-ai version: {pipecat.__version__}')"

# Run pending DB migrations if .env is present.
# Safe to skip if the DB isn't reachable yet — the runner will surface a clear error
# and the user can re-run `uv run python scripts/migrate.py up` after configuring it.
if [ -f .env ]; then
  echo "Running pending DB migrations..."
  uv run python scripts/migrate.py up || echo "⚠️  Migrations did not complete. Check POSTGRES_* in .env and run 'uv run python scripts/migrate.py up' once your DB is reachable."
else
  echo "ℹ️  No .env found — skipping migrations. Copy .env.example to .env, fill POSTGRES_*, then run 'uv run python scripts/migrate.py up'."
fi

echo "✅ Setup complete!"
