"""
ocr_cleanup.py -- Fix common OCR errors from macOS Vision framework on Intel Macs.

The Vision framework on Intel (especially with VNRequestTextRecognitionLevelFast)
produces consistent, predictable error patterns. This module corrects them with
minimal overhead so it can run on every capture cycle.

Usage:
    from ocr_cleanup import OCRCleaner
    cleaner = OCRCleaner()
    clean_text = cleaner.clean(noisy_text)
    score = cleaner.confidence_score(noisy_text)
"""

from __future__ import annotations

import re
import logging
from typing import Dict, List, Tuple

log = logging.getLogger("claude-eyes.ocr-cleanup")


# ---------------------------------------------------------------------------
# Substitution tables
# ---------------------------------------------------------------------------

# Whole-word replacements (case-sensitive).
# Keys are the OCR-mangled form, values are the correct text.
WORD_SUBSTITUTIONS: Dict[str, str] = {
    # Capital N replacing W
    "Nhen": "When",
    "Nith": "With",
    "Nhat": "What",
    "Nhere": "Where",
    "Nhich": "Which",
    "Nhile": "While",
    "Nhy": "Why",
    "Nho": "Who",
    "Nhose": "Whose",
    "Nhole": "Whole",
    "Nork": "Work",
    "Nord": "Word",
    "Norld": "World",
    "Nrite": "Write",
    "Nriter": "Writer",
    "Nritten": "Written",
    "Nrong": "Wrong",
    "Nrap": "Wrap",
    "Narning": "Warning",
    "Neb": "Web",
    "Nebsite": "Website",
    "Nindow": "Window",
    "Nindows": "Windows",
    "NoH": "Now",
    "NoN": "Now",
    "Nas": "Was",
    "Nere": "Were",
    "Nill": "Will",
    "Nant": "Want",
    "Nay": "Way",
    "Nait": "Wait",
    "Natch": "Watch",
    "Nalk": "Walk",
    # Capital H replacing W
    "Hdp": "Help",
    "Hhen": "When",
    "Hith": "With",
    "Hhat": "What",
    "Hhere": "Where",
    "Hork": "Work",
    "Hord": "Word",
    "Horld": "World",
    "Hindow": "Window",
    "Hindows": "Windows",
    # WindoH pattern (trailing H for w)
    "WindoH": "Window",
    "windoH": "window",
    "noH": "now",
    "hoH": "how",
    "HoH": "How",
    "shoH": "show",
    "ShoH": "Show",
    "floH": "flow",
    "FloH": "Flow",
    "knoH": "know",
    "KnoH": "Know",
    "beloH": "below",
    "BeloH": "Below",
    "alloH": "allow",
    "AlloH": "Allow",
    "folloH": "follow",
    "FolloH": "Follow",
    # l replacing I at start of sentences (lowercase L for uppercase I)
    "ln": "In",
    "lt": "It",
    "lf": "If",
    "ls": "Is",
    "lnstall": "Install",
    "lmport": "Import",
    "lnput": "Input",
    "lnit": "Init",
    "lnterface": "Interface",
    "lnternal": "Internal",
    "lnvalid": "Invalid",
    "ltem": "Item",
    "ltems": "Items",
    "lndex": "Index",
    "lnsert": "Insert",
    "lnfo": "Info",
    "lmage": "Image",
    "lcon": "Icon",
    # 0 replacing O in common words
    "0pen": "Open",
    "0ption": "Option",
    "0ptions": "Options",
    "0utput": "Output",
    "0bject": "Object",
    "0verride": "Override",
    "0K": "OK",
    # Common app names mangled by OCR
    "IT8rn12": "iTerm2",
    "IT8rnl2": "iTerm2",
    "IT8mi2": "iTerm2",
    "lTerm2": "iTerm2",
    "iTern2": "iTerm2",
    "Ifiew": "View",
    "IfieN": "View",
    "Hdp": "Help",
    "Nindow": "Window",
    "WirKIow": "Window",
    "WindoN": "Window",
    "Profi les": "Profiles",
    "Sessi on": "Session",
    "Scri pts": "Scripts",
    "COMMand": "command",
    "COMMIt": "commit",
    "COMpile": "compile",
    "COMpiler": "compiler",
    "COMplet": "complet",
    "COMMit": "commit",
    "PerMission": "Permission",
    "TlMeout": "Timeout",
    "ConnectTIMeout": "ConnectTimeout",
    "proiect": "project",
    "proiects": "projects",
    "Proiect": "Project",
    "Proiects": "Projects",
    # Common programming terms mangled
    "def7ne": "define",
    "funct7on": "function",
    "ret urn": "return",
    "pr7nt": "print",
    "7mport": "import",
    "7nstall": "install",
    "7nput": "input",
    "7nclude": "include",
    "7nit": "init",
    "7nterface": "interface",
    "7f": "if",
    "7n": "in",
    "7s": "is",
    "7t": "it",
    # ff ligature mangling (fi/fl → ff)
    "fflter": "filter",
    "ffle": "file",
    "ffnd": "find",
    "ffrst": "first",
    "ffnal": "final",
    "ffnally": "finally",
    "ffx": "fix",
    "ffll": "fill",
    "ffled": "filed",
    "ffles": "files",
    "ffght": "fight",
    "ffgure": "figure",
    "ffre": "fire",
    "ffnish": "finish",
    "fflow": "flow",
    "fflat": "flat",
    "fflag": "flag",
    "fflags": "flags",
    "ffloor": "floor",
    "fflush": "flush",
    "ffly": "fly",
}

# Regex-based substitutions applied after word-level fixes.
# Each tuple: (compiled_pattern, replacement_string, description)
_REGEX_FIXES: List[Tuple[re.Pattern, str, str]] = []


def _build_regex_fixes() -> List[Tuple[re.Pattern, str, str]]:
    """Build regex patterns once at import time."""
    fixes = []

    # J replacing / in filesystem paths: "JUsersJ" -> "/Users/"
    fixes.append((
        re.compile(r"J(Users|home|var|tmp|etc|opt|usr|bin|sbin|dev|proc|sys|Library|Applications|Volumes)J"),
        r"/\1/",
        "J-as-slash in paths",
    ))
    # Leading J before known path roots
    fixes.append((
        re.compile(r"(?<!\w)J(Users|home|var|tmp|etc|opt)(?=/|\b)"),
        r"/\1",
        "J-as-slash at path start",
    ))

    # 7 replacing i inside words (but NOT standalone "7" which is a real number).
    # Match words where 7 appears surrounded by letters.
    fixes.append((
        re.compile(r"(?<=[a-zA-Z])7(?=[a-zA-Z])"),
        "i",
        "7-as-i inside words",
    ))

    # 0 replacing O inside words (surrounded by letters on both sides).
    fixes.append((
        re.compile(r"(?<=[a-zA-Z])0(?=[a-zA-Z])"),
        "O",
        "0-as-O inside words",
    ))

    # Doubled consonant noise: "tthe" -> "the", "ffor" -> "for"
    # Only fix at word boundaries and only for common doubled-start patterns.
    for doubled in ["tthe", "ffor", "iin", "oof", "oon", "aand", "oor", "iis", "tto"]:
        fixes.append((
            re.compile(r"\b" + doubled + r"\b", re.IGNORECASE),
            doubled[1:],  # drop the doubled first char
            f"doubled-start: {doubled}",
        ))

    return fixes


_REGEX_FIXES = _build_regex_fixes()


# ---------------------------------------------------------------------------
# Pre-compiled word boundary pattern for fast whole-word replacement
# ---------------------------------------------------------------------------

# Build a single alternation regex from all word substitutions for speed.
# Sort by length descending so longer matches take priority.
_sorted_keys = sorted(WORD_SUBSTITUTIONS.keys(), key=len, reverse=True)
_WORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _sorted_keys) + r")\b"
)

# Patterns used to estimate OCR error density (for confidence scoring).
_ERROR_INDICATORS: List[re.Pattern] = [
    re.compile(r"\bN[a-z]{2,}\b"),              # Capital N before lowercase (N-for-W)
    re.compile(r"(?<=[a-zA-Z])7(?=[a-zA-Z])"),   # 7-as-i
    re.compile(r"(?<=[a-zA-Z])0(?=[a-zA-Z])"),   # 0-as-O
    re.compile(r"J(?:Users|home|var)"),            # J-as-slash
    re.compile(r"\bH[a-z]{2,}\b"),                # Capital H before lowercase (H-for-W)
    re.compile(r"[a-z]H\b"),                       # trailing H-for-w
    re.compile(r"\bff[a-z]{2,}\b"),               # ff ligature mangling
    re.compile(r"\bl[A-Z][a-z]"),                  # l-for-I at word start
]


# ---------------------------------------------------------------------------
# OCRCleaner
# ---------------------------------------------------------------------------

class OCRCleaner:
    """
    Cleans common OCR errors produced by macOS Vision framework on Intel Macs.

    Designed to be instantiated once and reused. All heavy regex compilation
    happens at module import time, so .clean() is just pattern matching.

    Usage:
        cleaner = OCRCleaner()
        clean_text = cleaner.clean(raw_ocr_output)
        quality = cleaner.confidence_score(raw_ocr_output)
    """

    def __init__(self, extra_substitutions: Dict[str, str] | None = None):
        """
        Args:
            extra_substitutions: Optional dict of additional word-level
                replacements to apply on top of the built-in table.
        """
        self._word_subs = dict(WORD_SUBSTITUTIONS)
        self._word_pattern = _WORD_PATTERN

        if extra_substitutions:
            self._word_subs.update(extra_substitutions)
            # Rebuild the alternation pattern with the extended table.
            keys = sorted(self._word_subs.keys(), key=len, reverse=True)
            self._word_pattern = re.compile(
                r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b"
            )

    def clean(self, text: str) -> str:
        """
        Apply all OCR error corrections to the input text.

        Runs in two passes:
          1. Whole-word dictionary substitutions (single regex alternation).
          2. Regex-based contextual fixes (7-in-words, J-as-slash, etc.).

        Returns the corrected text. If the input is empty or None, returns
        an empty string.
        """
        if not text:
            return ""

        # Pass 1: whole-word substitutions via single compiled alternation.
        result = self._word_pattern.sub(
            lambda m: self._word_subs[m.group(0)], text
        )

        # Pass 2: regex-based contextual fixes.
        for pattern, replacement, _desc in _REGEX_FIXES:
            result = pattern.sub(replacement, result)

        return result

    def confidence_score(self, text: str) -> float:
        """
        Estimate OCR output quality on a 0.0-1.0 scale.

        1.0 = no detected errors (high confidence the text is clean).
        0.0 = heavily corrupted.

        The score is based on the density of known error patterns relative
        to total word count. A text with zero recognized error patterns
        scores 1.0. Each detected error pattern reduces the score
        proportionally.

        This is a heuristic, not ground truth. It catches the patterns
        this module knows about, not arbitrary OCR mistakes.
        """
        if not text or not text.strip():
            return 0.0

        words = text.split()
        word_count = len(words)
        if word_count == 0:
            return 0.0

        # Count total error pattern matches.
        error_count = 0
        for pattern in _ERROR_INDICATORS:
            error_count += len(pattern.findall(text))

        # Also count word-level substitution hits.
        error_count += len(self._word_pattern.findall(text))

        # Compute error density: errors per word.
        error_density = error_count / word_count

        # Map density to a 0-1 score.
        # 0 errors -> 1.0
        # density >= 0.3 (30% of words are errors) -> ~0.0
        # Linear interpolation clamped to [0.0, 1.0].
        score = max(0.0, min(1.0, 1.0 - (error_density / 0.3)))
        return round(score, 3)

    def clean_with_stats(self, text: str) -> Tuple[str, Dict[str, int]]:
        """
        Like clean(), but also returns a dict of fix categories and counts.
        Useful for debugging and tuning.

        Returns:
            (cleaned_text, {"word_subs": N, "regex_fixes": N, ...})
        """
        if not text:
            return "", {}

        stats: Dict[str, int] = {}

        # Pass 1: word substitutions.
        word_hits = self._word_pattern.findall(text)
        stats["word_substitutions"] = len(word_hits)
        result = self._word_pattern.sub(
            lambda m: self._word_subs[m.group(0)], text
        )

        # Pass 2: regex fixes, counted individually.
        total_regex = 0
        for pattern, replacement, desc in _REGEX_FIXES:
            hits = len(pattern.findall(result))
            if hits > 0:
                stats[f"regex:{desc}"] = hits
                total_regex += hits
            result = pattern.sub(replacement, result)

        stats["regex_fixes_total"] = total_regex

        return result, stats

    def add_substitution(self, bad: str, good: str) -> None:
        """
        Add a single word substitution at runtime.
        Rebuilds the internal pattern to include the new entry.
        """
        self._word_subs[bad] = good
        keys = sorted(self._word_subs.keys(), key=len, reverse=True)
        self._word_pattern = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b"
        )

    def add_substitutions(self, mapping: Dict[str, str]) -> None:
        """
        Add multiple word substitutions at runtime.
        Rebuilds the internal pattern once after all entries are added.
        """
        self._word_subs.update(mapping)
        keys = sorted(self._word_subs.keys(), key=len, reverse=True)
        self._word_pattern = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b"
        )


# ---------------------------------------------------------------------------
# Module-level convenience (optional singleton)
# ---------------------------------------------------------------------------

_default_cleaner: OCRCleaner | None = None


def get_cleaner() -> OCRCleaner:
    """Return a module-level singleton OCRCleaner instance."""
    global _default_cleaner
    if _default_cleaner is None:
        _default_cleaner = OCRCleaner()
    return _default_cleaner


# ---------------------------------------------------------------------------
# Quick self-test when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_cases = [
        ("Nhen you 7nstall the f7le, check the Nindow", "When you install the file, check the Window"),
        ("JUsersJjames/Desktop", "/Users/james/Desktop"),
        ("Nhat 7s th7s NoH?", "What is this Now?"),
        ("0pen the 0ption menu", "Open the Option menu"),
        ("lmport the ltem from the ffle", "Import the Item from the file"),
        ("WindoH closed noH", "Window closed now"),
        ("Nrite the funct7on", "Write the function"),
        ("Hork on the Horld project", "Work on the World project"),
    ]

    cleaner = OCRCleaner()

    print("OCR Cleanup Self-Test")
    print("=" * 70)

    all_passed = True
    for raw, expected in test_cases:
        result = cleaner.clean(raw)
        score = cleaner.confidence_score(raw)
        passed = result == expected
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False

        print(f"\n[{status}] Score: {score:.3f}")
        print(f"  Input:    {raw}")
        print(f"  Expected: {expected}")
        print(f"  Got:      {result}")

    print("\n" + "=" * 70)
    print(f"Result: {'All tests passed' if all_passed else 'Some tests FAILED'}")

    # Show stats for a sample
    sample = "Nhen you 7nstall the f7le from JUsersJjames, check the Nindow noH"
    cleaned, stats = cleaner.clean_with_stats(sample)
    print(f"\nStats example:")
    print(f"  Input:   {sample}")
    print(f"  Cleaned: {cleaned}")
    print(f"  Stats:   {stats}")
    print(f"  Score:   {cleaner.confidence_score(sample):.3f}")
