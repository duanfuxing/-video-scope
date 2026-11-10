import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_document_links_and_json_baselines() -> None:
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    baselines = 0
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in target:
                assert (document.parent / target.split("#")[0]).exists(), (document, target)
        for block in re.findall(r"```json\n(.*?)\n```", text, re.S):
            assert isinstance(json.loads(block), dict)
            if document.name.endswith("JSON.md"):
                baselines += 1
    assert baselines == 2
