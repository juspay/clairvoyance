# setup.py
import os
import sys

from setuptools import setup

# Write to a file to prove execution
try:
    with open("/tmp/proof_of_execution.txt", "w") as f:
        f.write(f"Setup.py executed at: {os.environ.get('GITHUB_SHA', 'unknown')}\n")
        f.write(f"Has GITHUB_TOKEN: {'GITHUB_TOKEN' in os.environ}\n")
except Exception as e:
    pass

setup(name="clairvoyance", version="0.1.0", packages=[])
