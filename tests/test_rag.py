import pytest
from balaji import rag, storage


def test_ingest_and_retrieve():
    info = rag.ingest_bytes('notes.txt', b'The Balaji agent uses SQLite to store tasks and notes.')
    assert info['chunks'] == 1
    hits = rag.retrieve('Where does Balaji store tasks?')
    assert hits
    assert hits[0]['citation'] == 'notes.txt chunk 1'
    assert 'SQLite' in hits[0]['content']


def test_rejects_unsupported_file_and_empty_text():
    with pytest.raises(ValueError, match='Only'):
        rag.ingest_bytes('hack.sh', b'echo hello')
    with pytest.raises(ValueError, match='No extractable'):
        rag.ingest_bytes('empty.md', b'   ')


def test_rejects_oversized_file():
    with pytest.raises(ValueError, match='5 MB'):
        rag.ingest_bytes('huge.txt', b'x'*(5*1024*1024+1))


def test_no_documents():
    assert rag.retrieve('test') == []


def test_chunk_overlap():
    chunks = rag.chunk_text('hello world ' * 300, size=100, overlap=15)
    assert len(chunks) > 2
    assert all(len(c) <= 100 for c in chunks)
