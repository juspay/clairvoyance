#!/usr/bin/env python3
"""
Simple version management for CI/CD.
Usage: python version.py [patch|minor|major|set X.Y.Z]
"""

import re
import sys
from pathlib import Path


def update_version(new_version):
    """Update version in __version__.py"""
    version_file = Path("app/__version__.py")
    content = version_file.read_text()
    updated = re.sub(r'__version__ = "[^"]*"', f'__version__ = "{new_version}"', content)
    version_file.write_text(updated)
    print(new_version)


def get_current_version():
    """Get current version"""
    version_file = Path("app/__version__.py")
    content = version_file.read_text()
    match = re.search(r'__version__ = "([^"]*)"', content)
    return match.group(1) if match else "0.0.0"


def main():
    if len(sys.argv) < 2:
        print(get_current_version())
        return
    
    command = sys.argv[1]
    current = get_current_version()
    major, minor, patch = map(int, current.split('.'))
    
    if command == "patch":
        update_version(f"{major}.{minor}.{patch + 1}")
    elif command == "minor":
        update_version(f"{major}.{minor + 1}.0")
    elif command == "major":
        update_version(f"{major + 1}.0.0")
    elif command == "set" and len(sys.argv) > 2:
        update_version(sys.argv[2])
    else:
        print(current)


if __name__ == '__main__':
    main()