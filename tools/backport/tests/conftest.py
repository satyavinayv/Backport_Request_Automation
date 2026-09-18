"""
Shared pytest configuration.
Adds the tools/backport directory to sys.path so all modules can be imported
without installing the package, regardless of where pytest is invoked from.
"""
import sys
import os

# Insert the backport root so imports like `from config import ...` resolve correctly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
