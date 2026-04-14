# RECON -- Knowledge Extraction Pipeline

Extracts structured knowledge from PDFs and web content into a Qdrant vector database for RAG retrieval by Aurora.

## Quick Start

```bash
# Activate
cd /opt/recon && source venv/bin/activate

# Scan library for new PDFs
recon scan

# Queue and process
recon queue
recon extract
recon enrich
recon embed

# Or run full pipeline
recon run

# Ingest a web page
recon ingest-url "https://example.com/article" --category "Category" --process

# Crawl an entire docs site
recon crawl "https://docs.example.com" --include /docs/ --category "Category" --process

# Upload a PDF
recon upload --file /path/to/document.pdf --category "Category"

# Search
recon search "water purification methods"

# Check status
recon status
recon failures
```

## Dashboard

http://100.64.0.24:8420

## Services

| Service | Location | Purpose |
|---------|----------|---------|
| RECON Dashboard | recon:8420 | Pipeline management + API |
| Qdrant | cortex:6333 | Vector database |
| TEI | cortex:8090 | Embeddings (1,711/sec) |
| Ollama | cortex:11434 | Chat + fallback embeddings |
| OpenWebUI | cortex:8080 (ai.echo6.co) | Aurora chat with RAG |
| File Server | recon:8888 (files.echo6.co) | PDF downloads |

## Key Paths

| Path | Contents |
|------|----------|
| /opt/recon/ | Application code |
| /opt/recon/data/concepts/ | Gemini extractions (**CRITICAL -- back these up**) |
| /opt/recon/data/text/ | Extracted text |
| /opt/recon/data/recon.db | SQLite status DB |
| /mnt/library/ | PDF library (NFS from pi-nas) |

## Backups

Automated every 6 hours to Contabo VPS via `/opt/recon/scripts/backup.sh`.
Concept JSONs are the most valuable data ($130+ of Gemini API work).
Qdrant is NOT backed up -- rebuilt from JSONs in ~10 minutes via `recon rebuild`.

## Monitoring

```bash
# Pipeline status
recon status

# Tail logs
tail -f /opt/recon/logs/recon.log

# Pipeline run log
tail -f /opt/recon/pipeline.log

# Validate consistency
recon validate --deep
```

## Full Documentation

See [PROJECT-BIBLE.md](PROJECT-BIBLE.md) for complete system documentation.
