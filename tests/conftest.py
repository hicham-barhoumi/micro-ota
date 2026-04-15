"""Shared pytest fixtures."""

import os
import pytest


@pytest.fixture(autouse=True)
def restore_cwd():
    """Restore the working directory after each test.

    Several tests chdir into a TemporaryDirectory.  Without this fixture
    the cwd ends up pointing at a deleted directory, causing subsequent
    os.getcwd() calls to raise FileNotFoundError.
    """
    original = os.getcwd()
    yield
    os.chdir(original)
