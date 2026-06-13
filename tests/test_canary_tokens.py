#!/usr/bin/env python3
"""Test suite for canary_tokens.py — token minting and echo detection."""
import sys
import unittest
from pathlib import Path

scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

import canary_tokens


class TestCanaryTokenMinting(unittest.TestCase):
    """Test mint_token() generation."""

    def test_mint_token_format(self):
        """mint_token() should return CN-<hex>-<hex> format."""
        token = canary_tokens.mint_token()
        parts = token.split("-")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "CN")
        self.assertEqual(len(parts[1]), 8)  # 4 bytes = 8 hex chars
        self.assertEqual(len(parts[2]), 8)
        # Verify all are hex
        for i in [1, 2]:
            int(parts[i], 16)  # Should not raise

    def test_mint_token_uniqueness(self):
        """Two consecutive mint_token() calls should return different tokens."""
        token1 = canary_tokens.mint_token()
        token2 = canary_tokens.mint_token()
        self.assertNotEqual(token1, token2)

    def test_mint_token_multiple_uniqueness(self):
        """Multiple token mints should all be unique."""
        tokens = [canary_tokens.mint_token() for _ in range(10)]
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_mint_token_non_empty(self):
        """mint_token() should never return empty string."""
        for _ in range(5):
            token = canary_tokens.mint_token()
            self.assertGreater(len(token), 0)
            self.assertNotEqual(token, "")


class TestCanaryTokenInjection(unittest.TestCase):
    """Test inject() function for embedding tokens in prompts."""

    def test_inject_basic(self):
        """inject() should append marker with token to prompt."""
        prompt = "Write a poem about cats."
        injected, token = canary_tokens.inject(prompt)
        self.assertIn(prompt, injected)
        self.assertIn(token, injected)
        self.assertGreater(len(token), 0)

    def test_inject_token_uniqueness(self):
        """inject() should mint a new token each time."""
        prompt = "Test prompt"
        _, token1 = canary_tokens.inject(prompt)
        _, token2 = canary_tokens.inject(prompt)
        self.assertNotEqual(token1, token2)

    def test_inject_custom_marker(self):
        """inject() should support custom marker template."""
        prompt = "Original prompt"
        marker = "MARKER: {token}"
        injected, token = canary_tokens.inject(prompt, marker=marker)
        self.assertIn(prompt, injected)
        self.assertIn("MARKER:", injected)
        self.assertIn(token, injected)

    def test_inject_marker_validation(self):
        """inject() should raise ValueError if marker lacks {token} placeholder."""
        prompt = "Test"
        with self.assertRaises(ValueError):
            canary_tokens.inject(prompt, marker="No token here")

    def test_inject_default_marker_format(self):
        """inject() should include 'do not echo' in default marker."""
        prompt = "Test"
        injected, _ = canary_tokens.inject(prompt)
        self.assertIn("do not echo", injected.lower())

    def test_inject_appends_to_prompt(self):
        """inject() should append marker on new line."""
        prompt = "Line 1\nLine 2"
        injected, _ = canary_tokens.inject(prompt)
        # Original prompt should be followed by blank lines then marker
        self.assertIn(prompt, injected)
        self.assertTrue(injected.startswith(prompt))


class TestCanaryTokenEchoDetection(unittest.TestCase):
    """Test detect_echo() function."""

    def test_detect_echo_present(self):
        """detect_echo() should return True when token is in text."""
        token = "CN-12345678-87654321"
        text = f"The response includes {token} somewhere in the middle."
        self.assertTrue(canary_tokens.detect_echo(text, token))

    def test_detect_echo_absent(self):
        """detect_echo() should return False when token is not in text."""
        token = "CN-12345678-87654321"
        text = "The response does not include the token."
        self.assertFalse(canary_tokens.detect_echo(text, token))

    def test_detect_echo_empty_text(self):
        """detect_echo() should return False for empty text."""
        token = "CN-12345678-87654321"
        self.assertFalse(canary_tokens.detect_echo("", token))

    def test_detect_echo_empty_token(self):
        """detect_echo() should return False for empty token."""
        text = "Some response"
        self.assertFalse(canary_tokens.detect_echo(text, ""))

    def test_detect_echo_both_empty(self):
        """detect_echo() should return False when both are empty."""
        self.assertFalse(canary_tokens.detect_echo("", ""))

    def test_detect_echo_partial_match_no(self):
        """detect_echo() should use exact literal match, not substring."""
        token = "CN-12345678-87654321"
        text = "Response with CN-12345678 but not full token."
        self.assertFalse(canary_tokens.detect_echo(text, token))

    def test_detect_echo_case_sensitive(self):
        """detect_echo() should be case-sensitive."""
        token = "CN-aaaaaaaa-bbbbbbbb"
        text = "Response with cn-aaaaaaaa-bbbbbbbb (lowercase)."
        self.assertFalse(canary_tokens.detect_echo(text, token))


class TestCanaryTokenIntegration(unittest.TestCase):
    """Test end-to-end inject + detect workflow."""

    def test_inject_then_detect_success(self):
        """Should detect token when prompt is echoed back."""
        prompt = "Write a story."
        injected, token = canary_tokens.inject(prompt)
        # Simulate model echoing the injected prompt
        response = injected
        self.assertTrue(canary_tokens.detect_echo(response, token))

    def test_inject_then_detect_normal_response(self):
        """Should not detect token in normal response without echo."""
        prompt = "Write a story."
        injected, token = canary_tokens.inject(prompt)
        # Simulate model responding normally
        response = "Once upon a time, there was a brave knight..."
        self.assertFalse(canary_tokens.detect_echo(response, token))

    def test_multiple_inject_cycles(self):
        """Should support multiple inject-detect cycles."""
        for _ in range(3):
            prompt = "Another test prompt"
            injected, token = canary_tokens.inject(prompt)
            self.assertIn(token, injected)
            self.assertTrue(canary_tokens.detect_echo(injected, token))


if __name__ == "__main__":
    unittest.main()
