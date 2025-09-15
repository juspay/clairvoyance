#!/bin/sh

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d " " -f 2)
MAJOR_VERSION=$(echo "$PYTHON_VERSION" | cut -d "." -f 1)
MINOR_VERSION=$(echo "$PYTHON_VERSION" | cut -d "." -f 2)

if [ "$MAJOR_VERSION" -lt 3 ] || { [ "$MAJOR_VERSION" -eq 3 ] && [ "$MINOR_VERSION" -lt 10 ]; }; then
  echo "Error: Python version 3.10 or greater is required."
  echo "Found Python version $PYTHON_VERSION"
  exit 1
fi

echo "Python version check passed ($PYTHON_VERSION)."

# Setup Git hooks path to use .githooks directory
echo "Configuring git hooks..."
git config core.hooksPath .githooks

echo "Git hooks path set to .githooks/"
echo "✅ Pre-commit hook is now active."

echo "Checking pipecat version..."
pip show pipecat-ai
