"""
Vercel serverless entry point.
Imports the Flask app from main.py (one directory up).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app  # noqa: F401 – Vercel looks for `app`
