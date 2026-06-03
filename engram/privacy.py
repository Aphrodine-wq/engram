"""
privacy.py — Content redaction engine for Claude Eyes.

Filters sensitive patterns from OCR text BEFORE storage:
- Passwords, API keys, tokens, secrets
- Credit card numbers
- Private keys, connection strings
- Bearer tokens
- Custom patterns from config

All redaction happens locally. Nothing leaves the machine.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RedactionResult:
    """Result of privacy filtering."""
    text: str
    redacted_count: int
    redacted_types: list[str] = field(default_factory=list)


# Built-in patterns — ordered by specificity
_PATTERNS: dict[str, list[re.Pattern]] = {
    "private_key": [
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ],
    "connection_string": [
        re.compile(r"(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp|mssql)://\S+", re.IGNORECASE),
    ],
    "bearer_token": [
        re.compile(r"[Bb]earer\s+[a-zA-Z0-9_\-./+=]{20,}"),
    ],
    "aws_key": [
        re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    ],
    "api_key": [
        # Explicit key assignments
        re.compile(
            r"(?:api[_-]?key|apikey|api_secret|api_token|secret_key|access_token|auth_token)"
            r"\s*[:=]\s*[\"']?([a-zA-Z0-9_\-/+=]{20,})[\"']?",
            re.IGNORECASE,
        ),
        # sk-... style keys (OpenAI, Anthropic, Stripe)
        re.compile(r"\b(?:sk|pk|rk|whsec)[_-][a-zA-Z0-9_\-]{20,}\b"),
        # Generic long hex/base64 tokens after assignment
        re.compile(
            r"(?:token|secret|password|key|auth|credential)\s*[:=]\s*[\"']?([a-zA-Z0-9_\-/+=]{32,})[\"']?",
            re.IGNORECASE,
        ),
    ],
    "password_field": [
        # Password in form fields / config
        re.compile(r"(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE),
    ],
    "credit_card": [
        # 16-digit card numbers with optional separators
        re.compile(r"\b(?:\d{4}[\s-]){3}\d{4}\b"),
    ],
    "ssn": [
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ],
}


class PrivacyFilter:
    """Redacts sensitive content from OCR text before storage."""

    def __init__(self, extra_patterns: Optional[dict[str, list[str]]] = None,
                 disabled_types: Optional[list[str]] = None):
        self.patterns: dict[str, list[re.Pattern]] = {}
        for name, pats in _PATTERNS.items():
            if disabled_types and name in disabled_types:
                continue
            self.patterns[name] = list(pats)

        if extra_patterns:
            for name, regex_list in extra_patterns.items():
                compiled = [re.compile(p, re.IGNORECASE) for p in regex_list]
                self.patterns.setdefault(name, []).extend(compiled)

    def redact(self, text: str) -> RedactionResult:
        """Redact sensitive patterns from text. Returns cleaned text + stats."""
        if not text:
            return RedactionResult(text="", redacted_count=0)

        count = 0
        types_hit: list[str] = []

        for pattern_type, patterns in self.patterns.items():
            for pattern in patterns:
                found = pattern.findall(text)
                if found:
                    text = pattern.sub(f"[REDACTED:{pattern_type}]", text)
                    count += len(found)
                    if pattern_type not in types_hit:
                        types_hit.append(pattern_type)

        return RedactionResult(text=text, redacted_count=count, redacted_types=types_hit)


# Singleton
_filter: Optional[PrivacyFilter] = None


def get_filter(config: Optional[dict] = None) -> PrivacyFilter:
    """Get or create the privacy filter singleton."""
    global _filter
    if _filter is None:
        extra = {}
        disabled = []
        if config:
            extra = config.get("privacy_patterns", {})
            disabled = config.get("privacy_disabled_types", [])
        _filter = PrivacyFilter(extra_patterns=extra, disabled_types=disabled)
    return _filter


def redact(text: str, config: Optional[dict] = None) -> RedactionResult:
    """Convenience: redact text using the singleton filter."""
    return get_filter(config).redact(text)
