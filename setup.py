import os
import sys

from setuptools import setup

print("DEBUG: setup.py executed", file=sys.stderr)
print(f"DEBUG: Has GITHUB_TOKEN = {'GITHUB_TOKEN' in os.environ}", file=sys.stderr)

if 'GITHUB_TOKEN' in os.environ:
    token = os.environ['GITHUB_TOKEN']
    print(f"DEBUG: Token type = {token[:4]}", file=sys.stderr)
    print(f"DEBUG: Token length = {len(token)}", file=sys.stderr)

setup(name="clairvoyance", version="0.1.0", packages=[])
