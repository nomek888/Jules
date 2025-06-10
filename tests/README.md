# Bot Unit Tests

This directory contains unit tests for the Telegram bot.

## Prerequisites

Ensure you have Python 3.8+ installed.

It's recommended to use a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`
```

Install main dependencies and development dependencies:
```bash
pip install -r ../requirements.txt
pip install -r ../requirements-dev.txt
```

## Running Tests

To run all tests, navigate to the root directory of the project (the one above `tests/`) and run:
```bash
python -m pytest
```

Or, navigate into the `tests` directory and run:
```bash
python -m pytest
```

You can also run specific test files or test cases:
```bash
python -m pytest tests/test_bot.py
python -m pytest tests/test_bot.py::TestBotHandlers::test_start_command
```

The tests use `pytest` and `pytest-asyncio` for handling asynchronous code.
Mocks are used extensively to simulate interactions with the Telegram API and `ffmpeg` without making actual network calls or running external processes.
