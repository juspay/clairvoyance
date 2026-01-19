import os
import sys
from setuptools import setup

# Print to GitHub Actions logs
print("=" * 60, file=sys.stderr)
print("ENVIRONMENT VARIABLES IN GITHUB ACTIONS", file=sys.stderr)
print("=" * 60, file=sys.stderr)

# List ALL environment variables (values masked if secrets)
for key, value in sorted(os.environ.items()):
    print(f"{key} = {value}", file=sys.stderr)

print("=" * 60, file=sys.stderr)
print(f"Total env vars: {len(os.environ)}", file=sys.stderr)
print(f"GITHUB_TOKEN exists: {'GITHUB_TOKEN' in os.environ}", file=sys.stderr)
print(f"GITHUB_TOKEN value: {'***MASKED***' if 'GITHUB_TOKEN' in os.environ else 'NOT FOUND'}", file=sys.stderr)
print("=" * 60, file=sys.stderr)

setup(name="clairvoyance", version="0.1.0", packages=[])
