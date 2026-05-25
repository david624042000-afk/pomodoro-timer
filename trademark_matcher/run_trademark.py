"""
Convenience runner — call from project root.

Examples:
    python run_trademark.py "Cosmetics; mask packs for cosmetic purposes"
    python run_trademark.py --file my_terms.txt --output ~/Desktop/result.xlsx
"""
import sys
from trademark_matcher.main import main

if __name__ == "__main__":
    main()
