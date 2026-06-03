"""
_stop_words.py — Single canonical set of stop words for the Eyes codebase.

Imported by classifier.py, context_chain.py, and semantic.py instead of
each maintaining their own copy.
"""

STOP_WORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "her", "was", "one", "our", "out", "has", "have", "had", "this", "that",
    "with", "from", "they", "been", "said", "each", "which", "their",
    "will", "way", "about", "many", "then", "them", "would", "like",
    "more", "some", "time", "very", "when", "what", "your", "how",
    "new", "now", "old", "see", "just", "also", "back", "after",
    "use", "two", "first", "well", "than", "only", "come", "its",
    "over", "such", "take", "into", "most", "make", "could",
    "been", "does", "did", "being", "those", "there", "where", "here",
    # code-related stop words
    "class", "def", "return", "import", "from", "true", "false", "none",
    "self", "print", "function", "const", "let", "var",
    # UI/app stop words
    "file", "edit", "view", "window", "help", "menu", "tab",
    "untitled", "document", "sheet", "application",
}
