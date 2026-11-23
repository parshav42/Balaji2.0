# BALAJI Project Knowledge Base

BALAJI is a local-first personal AI assistant for Linux. It runs a local large language model through Ollama. Its Python backend exposes a FastAPI REST API and an HTML dashboard.

BALAJI stores tasks, conversation history, approval requests, document chunks and saved memories in SQLite. It uses a fixed allowlist of tools and asks for explicit user approval before writing tasks or memories.

BALAJI supports a push-to-talk voice flow using whisper.cpp for speech recognition and Piper for speech output. Both speech systems can run offline after model downloads.

Document retrieval uses keyword matching by default. Optional nomic-embed-text embeddings can be generated locally by Ollama to enable semantic similarity search.
