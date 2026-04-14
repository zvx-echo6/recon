# RECON Project Bible v2.0

*Last updated: 2026-02-16*

---

## 1. Mission Statement

RECON (Reconnaissance, Extraction, Conceptualization, and Operationalization of kNowledge) is a knowledge extraction pipeline that processes PDFs and web content into structured concepts stored in a Qdrant vector database. These concepts power Aurora, the RAG-enabled AI assistant running on OpenWebUI.

**The core loop:** Content in (PDF/web) -> Text extracted -> Concepts enriched (Gemini) -> Vectors embedded (TEI/BGE-M3) -> Searchable knowledge (Qdrant) -> Aurora answers questions with citations.

---

## 2. Infrastructure

### Hosts

| Host | IP (Tailscale) | Role |
|------|---------------|------|
| recon LXC | 100.64.0.24 (CT 130 on toc) | RECON application, dashboard, pipeline |
| cortex VM | 100.64.0.14 (VM 150 on toc) | Qdrant, TEI, Ollama, OpenWebUI |
| pi-nas | 100.64.0.21 (192.168.1.245) | NFS file server for PDF library |
| Contabo VPS | 100.64.0.1 (5.189.158.149) | Backup destination |

### Services on cortex (100.64.0.14)

| Service | Port | Purpose |
|---------|------|---------|
| Qdrant | 6333 | Vector database (recon_knowledge collection) |
| TEI (text-embeddings-inference) | 8090 | Embedding server (bge-m3, 1024-dim, ~1,711 emb/sec) |
| Ollama | 11434 | LLM server + fallback embeddings (~8 emb/sec) |
| OpenWebUI | 8080 | Aurora chat interface (ai.echo6.co) |

### Services on recon LXC (100.64.0.24)

| Service | Port | Purpose |
|---------|------|---------|
| RECON Dashboard | 8420 | Web UI + API for pipeline management |
| File Server | 8888 | PDF downloads (files.echo6.co) |

### NFS Mount

```
pi-nas:/export/library -> /mnt/library (22TB, rw, NFSv3)
```

Contains ~13,000+ PDFs across:
- `Survival-Companion-Library/` (~12,900 PDFs in ~220 subdirectories)
- `Army_Pubs/` (~160 military field manuals)
- Other: `Gaming/`, `Reference/`, `Technical/`

---

## 3. Architecture Overview

```
                    /mnt/library/ (NFS)
                         |
                    [recon scan]
                         |
                    catalogue (SQLite)
                         |
                    [recon queue]
                         |
    +-----------+   [recon extract]   +-----------+
    |  PyPDF2   |-->  data/text/      |  Gemini   |
    | pdftotext |   {hash}/page_N.txt |  Flash    |
    | tesseract |        |            |  4 keys   |
    +-----------+   [recon enrich]    +-----------+
                         |
                    data/concepts/
                    {hash}/window_N.json
                         |
                    [recon embed]
                         |
              +----------+-----------+
              |   TEI (primary)      |
              |   bge-m3, 1024-dim   |
              |   1,711 emb/sec      |
              +----------+-----------+
                         |
                    Qdrant (cortex:6333)
                    recon_knowledge collection
                         |
                    Aurora (OpenWebUI)
                    RAG search + citations
```

### Web Content Path

```
    URL(s) ──> [recon ingest-url / crawl]
                         |
                    trafilatura extraction
                    chunk into ~2000-word pages
                         |
                    data/text/{hash}/page_N.txt
                    (enters at "extracted" status)
                         |
                    [enrich] -> [embed]
                    (same as PDF path)
```

---

## 4. Pipeline Stages

### Status Flow

```
catalogued -> queued -> extracting -> extracted -> enriching -> enriched -> embedding -> complete
                                                                                    \-> failed
```

Web content enters at `extracted` status (text already extracted by trafilatura).

### Stage Details

| Stage | Tool | Input | Output | Speed |
|-------|------|-------|--------|-------|
| Scan | `recon scan` | /mnt/library/*.pdf | catalogue table | ~13K PDFs in ~30 min |
| Queue | `recon queue` | catalogue entries | documents table (status=queued) | Instant |
| Extract | `recon extract` | PDF files | data/text/{hash}/page_NNNN.txt | 4 workers, ~200/hr |
| Enrich | `recon enrich` | Text pages (10-page windows) | data/concepts/{hash}/window_N.json | 16 workers, 4 Gemini keys |
| Embed | `recon embed` | Concept JSONs | Qdrant vectors | TEI: 1,711 emb/sec |

### Extraction Fallback Chain

1. **PyPDF2** (fast, clean text) -> 2. **pdftotext** (handles complex layouts) -> 3. **Tesseract OCR** (scanned documents)

### Enrichment Details

- Model: `gemini-2.0-flash`
- Window size: 10 pages per API call (configurable)
- Workers: 16 concurrent (4 API keys x 4 workers each)
- Output format: JSON array of concept objects
- **CRITICAL**: Concept JSONs are saved to disk BEFORE any database operations
- Key rotation via `KeyRotator` class distributing across 4 Gemini API keys

### Embedding Details

- **Primary**: TEI at cortex:8090 (bge-m3 model, 1024 dimensions, ~1,711 embeddings/sec)
- **Fallback**: Ollama at cortex:11434 (bge-m3 model, ~8 embeddings/sec)
- Batch size: 128 embeddings per TEI request
- Distance metric: Cosine similarity
- **CRITICAL**: Dimensions are 1024 (bge-m3), NOT 384. Getting this wrong creates silent failures.

---

## 5. Directory Structure

```
/opt/recon/                          # Application root
  recon.py                           # CLI entry point
  config.yaml                        # Central configuration
  .env                               # Gemini API keys (4 keys)
  requirements.txt                   # Python dependencies
  PROJECT-BIBLE.md                   # This file
  README.md                          # Quick-start reference
  run-full-pipeline.sh               # Background pipeline runner

  lib/                               # Core modules
    __init__.py
    api.py                           # Flask web dashboard + API (port 8420)
    crawler.py                       # Site crawler (sitemap + BFS link-following)
    embedder.py                      # Concept -> vector embedding (TEI/Ollama -> Qdrant)
    enricher.py                      # Text -> concept extraction (Gemini)
    extractor.py                     # PDF -> text extraction (PyPDF2/pdftotext/OCR)
    ingester.py                      # ARGUS intel feed intake
    status.py                        # SQLite DB operations (catalogue + documents)
    utils.py                         # Config, hashing, URL generation, logging
    web_scraper.py                   # URL -> text extraction (trafilatura)

  scripts/                           # Operational scripts
    backup.sh                        # Automated backup to Contabo (cron every 6h)
    rebuild_qdrant.py                # Nuclear recovery: re-embed all concepts
    validate.py                      # Pipeline consistency validation

  data/                              # Pipeline data (on local disk)
    recon.db                         # SQLite status database
    text/                            # Extracted text
      {content_hash}/
        meta.json                    # Document metadata
        page_0001.txt                # Page text (4-digit, 1-indexed)
        page_0002.txt
        ...
    concepts/                        # Enriched concepts (**BACK THESE UP**)
      {content_hash}/
        window_1.json                # Concept JSON array (10-page window)
        window_2.json
        ...
    intel/                           # ARGUS intel feeds

  logs/                              # Application logs
    recon.log                        # Main rotating log
    backup.log                       # Backup operation log
    backup_cron.log                  # Cron backup log

  venv/                              # Python virtual environment
```

---

## 6. Database Schema

### SQLite (data/recon.db)

Two tables in WAL mode with thread-local connections.

#### catalogue

| Column | Type | Description |
|--------|------|-------------|
| hash | TEXT PK | MD5 content hash |
| filename | TEXT | Original filename |
| path | TEXT | Full filesystem path |
| size_bytes | INTEGER | File size |
| source | TEXT | Top-level directory (e.g., "Survival-Companion-Library") |
| category | TEXT | Second-level directory (e.g., "Bushcraft") |
| status | TEXT | "catalogued" or "processed" |
| discovered_at | TEXT | ISO timestamp |

#### documents

| Column | Type | Description |
|--------|------|-------------|
| hash | TEXT PK | MD5 content hash |
| filename | TEXT | Original filename |
| path | TEXT | Full path or URL |
| size_bytes | INTEGER | File/content size |
| page_count | INTEGER | Number of text pages |
| book_title | TEXT | Gemini-extracted title |
| book_author | TEXT | Gemini-extracted author |
| status | TEXT | Pipeline status |
| pages_extracted | INTEGER | Pages extracted |
| concepts_extracted | INTEGER | Concepts generated |
| vectors_inserted | INTEGER | Vectors in Qdrant |
| error_message | TEXT | Last error (if failed) |
| retry_count | INTEGER | Failure retry count |
| created_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

### Qdrant (cortex:6333)

Collection: `recon_knowledge`

| Field | Type | Description |
|-------|------|-------------|
| vector | float[1024] | BGE-M3 embedding |
| doc_hash | keyword | Links to SQLite document |
| filename | keyword | Source filename |
| book_title | keyword | Document title |
| book_author | keyword | Author name |
| source_type | keyword | "document", "web", or "intel_feed" |
| download_url | keyword | files.echo6.co URL or source URL |
| content | text | Concept text (searchable) |
| summary | text | Concept summary |
| title | keyword | Concept title |
| domain | keyword | Knowledge domain |
| subdomain | keyword | Knowledge subdomain |
| keywords | keyword[] | Concept keywords |
| skill_level | keyword | beginner/intermediate/advanced/expert |
| key_facts | text[] | Key facts list |
| scenario_applicable | text[] | Applicable scenarios |
| cross_domain_tags | keyword[] | Cross-references |
| chapter | keyword | Source chapter |
| page_ref | keyword | Source page reference |
| notes | text | Additional notes |
| _window | integer | Source window number |
| _start_page | integer | Starting page in document |
| verification_status | keyword | "unverified" (default) |
| credibility_score | float | 0.7 (default) |
| language | keyword | "en" (default) |

---

## 7. CLI Reference

```
recon <command> [options]
```

| Command | Description | Key Options |
|---------|-------------|-------------|
| `scan` | Scan library, catalogue new PDFs | `--path` |
| `queue` | Queue catalogued docs for processing | `--hash`, `--source`, `--category`, `--limit` |
| `extract` | Extract text from queued PDFs | `--workers` |
| `enrich` | Enrich extracted text via Gemini | `--workers`, `--limit` |
| `embed` | Embed concepts into Qdrant | `--workers`, `--limit` |
| `run` | Full pipeline (extract->enrich->embed) | `--workers`, `--enrich-workers`, `--limit` |
| `status` | Show pipeline status counts | |
| `catalogue` | Browse catalogue | `--sources`, `--categories`, `--source`, `--limit` |
| `failures` | Show failed documents | `--retry` |
| `search` | Semantic search | `query`, `--limit` |
| `upload` | Upload PDFs | `--file`, `--dir`, `--category` |
| `ingest-url` | Ingest web content | `url`, `--file`, `--category`, `--process` |
| `crawl` | Crawl a site | `url`, `--category`, `--include`, `--exclude`, `--max-pages`, `--dry-run`, `--process` |
| `validate` | Check pipeline consistency | `--deep` |
| `rebuild` | Rebuild Qdrant from concept JSONs | |
| `serve` | Start web dashboard (port 8420) | |
| `ingest` | Ingest ARGUS intel JSON | `--file`, `--directory` |

### Common Workflows

```bash
# Full library processing
recon scan && recon queue && recon run

# Ingest a single web page with full processing
recon ingest-url "https://example.com/article" --category "Reference" --process

# Dry-run crawl to preview URLs
recon crawl "https://docs.example.com" --include /docs/ --dry-run

# Full crawl with processing
recon crawl "https://docs.example.com" --include /docs/ --category "Reference" --process

# Upload a PDF
recon upload --file /path/to/document.pdf --category "Technical"

# Check what failed and retry
recon failures
recon failures --retry
```

---

## 8. Web Dashboard

### URL

```
http://100.64.0.24:8420
```

### Pages

| Route | Page | Description |
|-------|------|-------------|
| `/` | Dashboard | Knowledge base overview: document/concept/vector counts, source table, domain distribution bars, skill level breakdown, Qdrant health, recent completions, pipeline status |
| `/search` | Search | Semantic search with score bars, Web/PDF badges, download links |
| `/catalogue` | Catalogue | Browse all catalogued PDFs with source/category filters |
| `/upload` | Upload | PDF upload form with category datalist, recent uploads table |
| `/web-ingest` | Web Ingest | Two tabs: Single/Batch URL ingest, Site Crawl with preview |
| `/failures` | Failures | Failed documents with error messages and retry button |

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/search?q=...&limit=N` | Semantic search |
| GET | `/api/catalogue?source=...&limit=N` | Browse catalogue |
| GET | `/api/knowledge-stats` | Dashboard aggregation (totals, sources, domains, skills, Qdrant health) |
| POST | `/api/upload` | Upload PDF (multipart: file + category) |
| GET | `/api/upload/<hash>/status` | Check upload processing status |
| GET | `/api/upload/categories` | List available categories |
| POST | `/api/ingest-url` | Ingest single URL (json: url, category, process) |
| POST | `/api/ingest-urls` | Ingest multiple URLs (json: urls, category, process) |
| POST | `/api/crawl` | Crawl a site (json: url, category, include, exclude, max_pages, dry_run) |
| GET | `/api/crawl/<id>/status` | Poll crawl/pipeline progress |
| POST | `/api/failures/retry` | Re-queue all failed documents |

### Dashboard Features

- **Auto-refresh**: Every 30 seconds via JavaScript fetch
- **Knowledge cards**: Total documents, concepts, vectors, pages
- **Source table**: Per-source breakdown with document/concept/vector counts and PDF/WEB type badges
- **Domain distribution**: Horizontal bars showing top knowledge domains
- **Skill level breakdown**: beginner/intermediate/advanced/expert percentages
- **Qdrant health**: Connection status, points count, segments
- **Pipeline status**: Compact display of documents in each stage
- **Crawl polling**: Real-time stage tracking (ingesting -> enriching -> embedding)

---

## 9. Concept JSON Schema

Each window file (`data/concepts/{hash}/window_N.json`) contains a JSON array of concept objects:

```json
[
  {
    "title": "Water Purification Methods",
    "content": "Detailed text about the concept...",
    "summary": "Brief summary of the concept",
    "domain": "Survival",
    "subdomain": "Water",
    "keywords": ["purification", "filtration", "boiling"],
    "skill_level": "beginner",
    "key_facts": ["Boiling kills 99.9% of pathogens", "..."],
    "scenario_applicable": ["wilderness survival", "disaster preparedness"],
    "cross_domain_tags": ["health", "camping"],
    "chapter": "Chapter 3",
    "page_ref": "pp. 45-48",
    "notes": "Additional context or caveats",
    "_window": 1,
    "_start_page": 1
  }
]
```

---

## 10. Web Ingestion

### Single URL

```bash
recon ingest-url "https://example.com/article" --category "Reference" --process
```

Or via API:
```bash
curl -X POST http://100.64.0.24:8420/api/ingest-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article", "category": "Reference", "process": true}'
```

### Site Crawl

```bash
# Preview what would be crawled
recon crawl "https://docs.example.com" --include /docs/ --dry-run

# Full crawl
recon crawl "https://docs.example.com" --include /docs/ --category "Reference" --process
```

### How It Works

1. **URL discovery** (crawler.py):
   - Tries sitemap.xml first (preferred, finds all pages)
   - Falls back to BFS link-following if no sitemap
   - Filters by include/exclude patterns

2. **Content extraction** (web_scraper.py):
   - Uses trafilatura for clean text extraction
   - Chunks into ~2,000-word pages
   - Same output format as PDF extractor: `data/text/{hash}/page_NNNN.txt`
   - Content hash is MD5 of extracted text (deduplication)

3. **Pipeline integration**:
   - Web content enters at `extracted` status (no PDF extraction needed)
   - Enrichment and embedding proceed identically to PDF content
   - Qdrant vectors get `source_type: "web"` and `download_url` pointing to source URL

---

## 11. Configuration Reference

### config.yaml

```yaml
# Root path for the PDF library (NFS mount from pi-nas)
library_root: /mnt/library

processing:
  extract_workers: 4        # Concurrent PDF extraction threads
  enrich_workers: 16         # Concurrent Gemini enrichment threads (4 keys x 4)
  embed_workers: 4           # Concurrent embedding threads
  enrich_window_size: 5      # Pages per enrichment window (sent to Gemini)
  embed_batch_size: 500      # Vectors per Qdrant upsert batch
  rate_limit_delay: 0.1      # Delay between Gemini API calls (seconds)
  max_retries: 5             # Max retries for failed documents

embedding:
  backend: tei               # "tei" (primary, ~1,711 emb/sec) or "ollama" (fallback, ~8 emb/sec)
  tei_host: 100.64.0.14      # TEI server (cortex)
  tei_port: 8090             # TEI HTTP port
  ollama_host: 100.64.0.14   # Ollama server (cortex) — fallback only
  ollama_port: 11434         # Ollama HTTP port
  model: bge-m3              # Embedding model name
  dimensions: 1024           # CRITICAL: bge-m3 is 1024-dim, NOT 384
  batch_size: 128            # Embeddings per TEI batch request

vector_db:
  host: 100.64.0.14          # Qdrant server (cortex)
  port: 6333                 # Qdrant HTTP port
  collection: recon_knowledge  # Collection name

gemini:
  model: gemini-2.0-flash    # Gemini model for enrichment
  response_mime_type: application/json  # Force JSON output

web:
  port: 8420                 # Dashboard HTTP port
  host: 0.0.0.0              # Bind to all interfaces

paths:
  base: /opt/recon           # Application root
  data: /opt/recon/data      # Data directory
  text: /opt/recon/data/text  # Extracted text output
  concepts: /opt/recon/data/concepts  # Enriched concept JSONs
  intel: /opt/recon/data/intel  # ARGUS intel feeds
  logs: /opt/recon/logs      # Log files
  db: /opt/recon/data/recon.db  # SQLite database

book_server:
  base_url: https://files.echo6.co  # Public URL prefix for PDF downloads
  strip_prefix: /mnt/library  # Path prefix to strip when generating URLs

upload_paths:                 # Category -> filesystem path mapping for uploads
  Survival Reference: /mnt/library/Survival-Companion-Library/Uploads
  Military Doctrine: /mnt/library/Army_Pubs/Uploads
  Gaming: /mnt/library/Gaming
  Reference: /mnt/library/Reference
  Technical: /mnt/library/Technical
  default: /mnt/library      # Fallback for unknown categories

web_scraper:
  words_per_page: 2000       # Target words per page chunk
  fetch_timeout: 30          # HTTP request timeout (seconds)
  rate_limit_delay: 1.0      # Delay between URL fetches (seconds)
  max_batch_size: 50         # Max URLs per batch ingest
  user_agent: "Mozilla/5.0 (compatible; RECON/1.0)"

crawler:
  user_agent: "Mozilla/5.0 (compatible; RECON/1.0)"
  fetch_timeout: 30          # HTTP request timeout (seconds)
  rate_limit_delay: 1.0      # Delay between page fetches (seconds)
  max_pages: 500             # Max pages to discover per crawl
  max_depth: 3               # Max link-following depth (BFS only)
  default_exclude:            # URL patterns to always skip
    - /search
    - /404
    - /login
    - /signup
    - /auth/
    - /api/
    - /assets/
    - /static/
```

### .env

```
GEMINI_KEY_1=<key>
GEMINI_KEY_2=<key>
GEMINI_KEY_3=<key>
GEMINI_KEY_4=<key>
```

Four Gemini API keys rotated across 16 enrichment workers via `KeyRotator`.

---

## 12. Aurora RAG Integration

Aurora is the RAG-enabled AI assistant running on OpenWebUI (ai.echo6.co).

### How It Works

1. User asks a question in OpenWebUI
2. Aurora's OpenWebUI function/filter embeds the query via TEI (cortex:8090)
3. Searches Qdrant `recon_knowledge` collection for similar concepts
4. Top results are injected into the prompt as context
5. JOSIEFIED Qwen3 8B generates an answer with citations
6. Citations include `download_url` links (PDF files via files.echo6.co, web content via source URL)

### Key Components

- **Embedding**: Same TEI endpoint + bge-m3 model as RECON pipeline (ensures vector compatibility)
- **Search**: Cosine similarity, top-5 results by default
- **LLM**: `goekdenizguelmez/JOSIEFIED-Qwen3:8b` on Ollama (cortex:11434)
- **Citations**: Each result includes `download_url` — either `https://files.echo6.co/...` for PDFs or the original URL for web content

---

## 13. Backup & Recovery

### Automated Backups

**Script**: `/opt/recon/scripts/backup.sh`
**Destination**: Contabo VPS (`root@100.64.0.1:/opt/backups/recon/`)
**Schedule** (cron):
- Every 6 hours: Full backup (concepts, text, DB, config, intel)
- Every 2 hours (off-hours): SQLite DB snapshot only

### What's Backed Up

| Component | Size | Priority | Notes |
|-----------|------|----------|-------|
| data/concepts/ | ~11M | **CRITICAL** | $130+ of Gemini API work |
| data/text/ | ~203M | High | Hours to regenerate |
| data/recon.db | ~6.5M | **CRITICAL** | All pipeline state |
| config.yaml + .env | ~2K | Important | Configuration |
| data/intel/ | ~4K | Low | Intel feed data |

### What's NOT Backed Up

- **Qdrant vectors**: Rebuilt from concept JSONs in ~10 minutes via `recon rebuild`
- **PDF library**: Lives on pi-nas NFS, backed up separately
- **venv/**: Recreated from requirements.txt

### Recovery Procedures

```bash
# Restore from backup
scp -r root@100.64.0.1:/opt/backups/recon/concepts/ /opt/recon/data/concepts/
scp -r root@100.64.0.1:/opt/backups/recon/text/ /opt/recon/data/text/
scp root@100.64.0.1:/opt/backups/recon/recon_LATEST.db /opt/recon/data/recon.db

# Rebuild Qdrant vectors from concept JSONs
cd /opt/recon && source venv/bin/activate
python3 scripts/rebuild_qdrant.py
# Type REBUILD when prompted
```

---

## 14. Embedding Performance

### TEI (Primary) vs Ollama (Fallback)

| Metric | TEI (cortex:8090) | Ollama (cortex:11434) |
|--------|-------------------|----------------------|
| Speed | ~1,711 emb/sec | ~8 emb/sec |
| Model | bge-m3 | bge-m3 |
| Dimensions | 1024 | 1024 |
| Batch size | 128 | 1 |
| Cosine similarity | 0.999900 | 0.999900 |

TEI is ~214x faster than Ollama for embeddings. Always use TEI unless it's down.

### Qdrant Configuration

- Collection: `recon_knowledge`
- Distance: Cosine
- HNSW indexing threshold: 20,000 (below this, brute-force search is used)
- Current state: Brute-force (under 20K vectors) — this is normal and performant at current scale

---

## 15. Content Hashing

- **PDF content**: `MD5(file_bytes)` — stable across renames, detects exact duplicates
- **Web content**: `MD5(extracted_text)` — deduplicates by content, not URL
- Hash is used as the primary key in both SQLite tables and as the directory name for text/concept storage

---

## 16. Source Type Handling

| Source | Path Format | source_type | download_url | Badge |
|--------|-------------|-------------|--------------|-------|
| PDF | `/mnt/library/...` | document | `https://files.echo6.co/...` | PDF |
| Web | `https://...` | web | Original URL | Web |
| Intel | JSON feed | intel_feed | — | — |

The `generate_download_url()` function in utils.py handles the routing:
- URLs starting with `http://` or `https://` are returned as-is
- File paths are converted to `files.echo6.co` URLs

---

## 17. Lessons Learned

### RECON Rebuild Lessons

1. **Verify infrastructure before writing code.** Check Qdrant, TEI, Ollama connectivity first.
2. **Dimensions are 1024, NOT 384.** BGE-M3 uses 1024-dimensional vectors. This caused silent failures in early builds.
3. **TEI >> Ollama for embeddings.** 1,711 vs 8 embeddings/sec. A 214x speedup that makes batch processing viable.
4. **Dynamic discovery over hardcoded paths.** Let the pipeline discover what's on disk rather than maintaining static file lists.
5. **Web content uses the same pipeline.** After text extraction, web and PDF content follow identical enrichment and embedding paths.
6. **Sitemap > link-following.** Sitemaps discover all pages reliably; BFS link-following misses orphaned pages and is slower.
7. **Save to disk before DB operations.** Concept JSONs are written to disk first, then the database is updated. This means recovery is always possible from the JSON files.
8. **NFS over large file sets is slow.** Scanning 13K PDFs over NFS takes ~30 minutes due to MD5 hashing over the network. Plan accordingly.

### Operational Gotchas

- `recon scan` can appear stuck on large PDFs over NFS — it's hashing, not hung
- Some PDFs have corrupt metadata that crashes PyPDF2 — the extractor catches this and falls back
- Gemini rate limits hit with 16 workers — the `KeyRotator` distributes across 4 keys to mitigate
- `iptables-persistent` hangs on interactive prompts in LXC containers — use manual persistence
- The recon LXC has no tmux/screen — use `nohup` for long-running background tasks

---

## 18. Monitoring

### Pipeline Status

```bash
# Quick status
recon status

# Dashboard
http://100.64.0.24:8420

# Tail logs
tail -f /opt/recon/logs/recon.log

# Pipeline run log (when running full background pipeline)
tail -f /opt/recon/pipeline.log
```

### Health Checks

```bash
# Qdrant
curl -s http://100.64.0.14:6333/collections/recon_knowledge | python3 -m json.tool

# TEI
curl -s http://100.64.0.14:8090/info

# Ollama
curl -s http://100.64.0.14:11434/api/tags | python3 -m json.tool

# NFS mount
df -h /mnt/library

# Backup logs
tail -20 /opt/recon/logs/backup.log
```

### Validation

```bash
# Quick validation
recon validate

# Deep validation (checks all files on disk)
recon validate --deep
```

---

## 19. Current State

*As of 2026-02-16*

### Pipeline Progress

| Status | Count |
|--------|-------|
| Catalogued | 10,162 |
| Queued | 8,982 |
| Extracted | 872 |
| Complete | 302 |
| Failed | 2 |

### Vector Database

- Qdrant points: 4,661 (3,144 PDF + 1,517 web)
- Segments: 8
- Indexing: Brute-force (under 20K threshold)

### Active Processing

Full pipeline running in background via `nohup` — extracting through the 8,982 queued documents. Expected to take ~40 hours for full extract -> enrich -> embed cycle.

### Backups

- Schedule: Every 6 hours (full) + every 2 hours (DB only)
- Destination: Contabo VPS (`/opt/backups/recon/`)
- Last verified: 2026-02-16 (220M total backup size)

---

## 20. Dependencies

### System Packages

- Python 3.11+
- pdftotext (poppler-utils)
- tesseract-ocr
- sqlite3

### Python Packages (key)

| Package | Version | Purpose |
|---------|---------|---------|
| Flask | 3.1.2 | Web dashboard |
| google-generativeai | 0.8.6 | Gemini API for enrichment |
| qdrant-client | 1.16.2 | Vector database client |
| PyPDF2 | 3.0.1 | PDF text extraction |
| trafilatura | 2.0.0 | Web content extraction |
| beautifulsoup4 | 4.14.3 | HTML parsing for crawler |
| lxml | 6.0.2 | XML/HTML parsing |
| pytesseract | 0.3.13 | OCR fallback |
| requests | 2.32.5 | HTTP client |
| PyYAML | 6.0.3 | Config file parsing |

Full list in `requirements.txt`.
