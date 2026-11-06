"""Compare the indexed text of two explicitly chosen document revisions, including their pages."""
import difflib
import re

import rag

# These characters can start Markdown, HTML, math, or citation syntax in a chat reply.
_literal = str.maketrans({char: f"&#{ord(char)};" for char in "&<>[]()!`*_~$\\|"})


def validate(collection, documents):
    rows = rag._read("SELECT id FROM documents WHERE collection_id=? AND id IN (?,?) AND state='ready'",
                     (collection, *documents))
    if len(rows) != 2:
        raise ValueError("Une version est absente de la collection ou son indexation est incomplète.")


def references(collection, documents):
    validate(collection, documents)
    rows = rag._read("SELECT d.collection_id,d.id AS doc_id,d.revision,c.id AS chunk_id FROM chunks c "
                     "JOIN documents d ON d.id=c.doc_id WHERE d.collection_id=? AND d.id IN (?,?) AND d.state='ready' "
                     "ORDER BY d.id,c.ord LIMIT 2001", (collection, *documents))
    if len(rows) > 2000 or {r["doc_id"] for r in rows} != set(documents):
        raise ValueError("La comparaison exige deux versions indexées, avec au plus 2 000 passages au total.")
    return [dict(r) for r in rows]


def _lines(hits):
    pages = []
    for hit in hits:
        page, text = hit["page"], hit["text"].removeprefix("[OCR] ")
        if not pages or pages[-1][0] != page:
            pages.append([page, text])
        else:
            previous = pages[-1][1]
            overlap = min(len(text), len(previous), rag.OVERLAP)
            while overlap and not previous.endswith(text[:overlap]):
                overlap -= 1
            pages[-1][1] = previous + ("" if overlap else " ") + text[overlap:]
    return [(page, line.strip()) for page, body in pages
            for line in re.split(r"\n+|(?<=[.!?])\s+", body) if line.strip()]


def compare(hits, documents):
    versions = [[h for h in hits if h["doc_id"] == did] for did in documents]
    if not all(versions):
        raise ValueError("Une version a changé, sélectionne à nouveau les deux documents.")
    left, right = (_lines(group) for group in versions)
    if max(len(left), len(right)) > 20000:
        raise ValueError("Limite de 20 000 lignes par version.")
    matcher = difflib.SequenceMatcher(None, [text for _, text in left], [text for _, text in right])
    changes = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    sources = [{"n": i, "title": group[0]["doc"] + (" · OCR" if any(h["text"].startswith("[OCR] ") for h in group) else "")}
               for i, group in enumerate(versions, 1)]
    if not changes:
        return "Les deux versions contiennent le même texte indexé. [1][2]", sources
    report = [f"{len(changes)} bloc(s) de texte modifié(s)."]
    remaining = 64000
    for n, (_, a, b, c, d) in enumerate(changes, 1):
        block = [f"\n### Modification {n}"]
        for label, number, lines in (("Avant", 1, left[a:b]), ("Après", 2, right[c:d])):
            for page, text in lines:
                where = f", p. {page}" if page else ""
                block.append(f"- {label} [{number}]{where} — {text.translate(_literal)}")
        section = "\n".join(block)
        report.append(section[:remaining])
        remaining -= len(section)
        if remaining <= 0:
            report.append("Aperçu limité à 64 000 caractères.")
            break
    return "\n".join(report), sources
